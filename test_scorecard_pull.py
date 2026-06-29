#!/usr/bin/env python3
"""
Tests for scorecard_pull.py

Covers:
  - _parse_iso helper (Python 3.6 datetime compatibility)
  - build_batches (field grouping and year-prefixing)
  - load_data_dictionary (real CSV parsing)
  - Checkpoint save/load and year-data save/load
  - Rate-limit logic (hold periods, window refresh)
  - CSV output (filename, column order, row count, values)
  - fetch_page (mocked requests: success, 429, retry, exhaustion)
  - main() integration: full run, rate-limit pause, resume from checkpoint,
    skipping completed years, scheduler install/remove

Run with pytest:
    pip install pytest
    pytest test_scorecard_pull.py -v

Run without pytest (plain unittest):
    python test_scorecard_pull.py
    python3 test_scorecard_pull.py        # Mac/Linux
    py test_scorecard_pull.py             # Windows
"""

import csv
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# ── Import the module under test ──────────────────────────────────────────────
# __file__ is always defined when this file is run via `python` or `%run`, and
# when it is imported as a module.  The NameError fallback covers the rare case
# of exec()-ing the source directly (e.g. copy-paste into a Jupyter cell).
try:
    sys.path.insert(0, str(Path(__file__).parent))
except NameError:
    sys.path.insert(0, str(Path.cwd()))
import scorecard_pull as sc

# ── Shared test data ──────────────────────────────────────────────────────────
MOCK_YEAR_FIELDS = [
    "admissions.admission_rate.overall",
    "admissions.sat_scores.25th_percentile.critical_reading",
    "cost.tuition.in_state",
    "completion.rate_suppressed.four_year",
]
MOCK_STATIC_FIELDS = ["id", "ope8_id", "school.name", "school.city"]


def _fake_records(fields, page, total=6, per_page=3):
    """Generate a realistic-looking page of API records for testing."""
    start = page * per_page
    end   = min(start + per_page, total)
    records = []
    for i in range(start, end):
        rec = {"id": 100000 + i}
        for f in fields:
            if f != "id":
                rec[f] = "val_{}_{}".format(i, f.split(".")[-1][:8])
        records.append(rec)
    return records, total


# ══════════════════════════════════════════════════════════════════════════════
class TestParseIso(unittest.TestCase):
    """_parse_iso() is our Python 3.6 replacement for datetime.fromisoformat()."""

    def test_parses_timestamp_with_microseconds(self):
        dt = sc._parse_iso("2024-03-15T14:30:45.123456")
        self.assertEqual(dt.year, 2024)
        self.assertEqual(dt.hour, 14)
        self.assertEqual(dt.microsecond, 123456)

    def test_parses_timestamp_without_microseconds(self):
        dt = sc._parse_iso("2024-03-15T14:30:45")
        self.assertEqual(dt.second, 45)
        self.assertEqual(dt.microsecond, 0)

    def test_roundtrip_with_datetime_isoformat(self):
        original = datetime(2025, 6, 1, 9, 5, 3, 654321)
        self.assertEqual(sc._parse_iso(original.isoformat()), original)

    def test_invalid_string_raises_value_error(self):
        with self.assertRaises(ValueError):
            sc._parse_iso("not-a-datetime")

    def test_invalid_string_raises_value_error_partial(self):
        with self.assertRaises(ValueError):
            sc._parse_iso("2024-13-99")


# ══════════════════════════════════════════════════════════════════════════════
class TestBuildBatches(unittest.TestCase):
    """build_batches() splits fields into URL-safe chunks for the API."""

    def test_id_is_first_field_in_every_batch(self):
        batches = sc.build_batches("2010", MOCK_YEAR_FIELDS, MOCK_STATIC_FIELDS)
        for i, batch in enumerate(batches):
            self.assertEqual(batch[0], "id",
                             msg="Batch {} does not start with 'id'".format(i))

    def test_batch_zero_contains_all_static_fields(self):
        batches = sc.build_batches("2010", MOCK_YEAR_FIELDS, MOCK_STATIC_FIELDS)
        for sf in MOCK_STATIC_FIELDS:
            self.assertIn(sf, batches[0])

    def test_year_fields_receive_correct_year_prefix(self):
        batches = sc.build_batches("1999", MOCK_YEAR_FIELDS, MOCK_STATIC_FIELDS)
        all_year_fields = [f for batch in batches[1:] for f in batch if f != "id"]
        for field in MOCK_YEAR_FIELDS:
            self.assertIn("1999.{}".format(field), all_year_fields)

    def test_different_years_produce_different_prefixes(self):
        b96 = sc.build_batches("1996", MOCK_YEAR_FIELDS, MOCK_STATIC_FIELDS)
        b24 = sc.build_batches("2024", MOCK_YEAR_FIELDS, MOCK_STATIC_FIELDS)
        fields_96 = {f for b in b96[1:] for f in b if f != "id"}
        fields_24 = {f for b in b24[1:] for f in b if f != "id"}
        self.assertTrue(all(f.startswith("1996.") for f in fields_96))
        self.assertTrue(all(f.startswith("2024.") for f in fields_24))

    def test_batch_size_cap_is_respected(self):
        many = ["admissions.field_{}".format(i) for i in range(200)]
        with patch.object(sc, "FIELD_BATCH_SIZE", 30):
            batches = sc.build_batches("2000", many, MOCK_STATIC_FIELDS)
        for batch in batches[1:]:
            year_only = [f for f in batch if f != "id"]
            self.assertLessEqual(len(year_only), 30)

    def test_no_year_fields_lost_across_batches(self):
        expected = {"2005.{}".format(f) for f in MOCK_YEAR_FIELDS}
        batches  = sc.build_batches("2005", MOCK_YEAR_FIELDS, MOCK_STATIC_FIELDS)
        got      = {f for b in batches[1:] for f in b if f != "id"}
        self.assertEqual(got, expected)

    def test_id_appears_exactly_once_in_static_batch(self):
        batches = sc.build_batches("2010", [], MOCK_STATIC_FIELDS)
        self.assertEqual(batches[0].count("id"), 1)

    def test_empty_year_fields_yields_only_static_batch(self):
        batches = sc.build_batches("2010", [], MOCK_STATIC_FIELDS)
        self.assertEqual(len(batches), 1)


# ══════════════════════════════════════════════════════════════════════════════
class TestDataDictionary(unittest.TestCase):
    """load_data_dictionary() against the real CSV on disk."""

    @classmethod
    def setUpClass(cls):
        cls.year_fields, cls.static_fields = sc.load_data_dictionary()

    def test_year_fields_list_is_nonempty(self):
        self.assertGreater(len(self.year_fields), 100)

    def test_static_fields_list_is_nonempty(self):
        self.assertGreater(len(self.static_fields), 10)

    def test_id_is_in_static_fields(self):
        self.assertIn("id", self.static_fields)

    def test_school_name_is_in_static_fields(self):
        self.assertIn("school.name", self.static_fields)

    def test_year_fields_use_valid_category_prefix(self):
        for f in self.year_fields[:100]:   # spot-check first 100
            cat = f.split(".")[0]
            self.assertIn(cat, sc.YEAR_CATS,
                          msg="Unexpected prefix in year field: {}".format(f))

    def test_no_duplicate_year_fields(self):
        self.assertEqual(len(self.year_fields), len(set(self.year_fields)))

    def test_no_duplicate_static_fields(self):
        self.assertEqual(len(self.static_fields), len(set(self.static_fields)))

    def test_no_blank_field_names(self):
        for f in self.year_fields + self.static_fields:
            self.assertTrue(f.strip(), msg="Blank field name found")

    def test_school_category_fields_have_school_prefix(self):
        school_fields = [f for f in self.static_fields if f.startswith("school.")]
        self.assertGreater(len(school_fields), 5)


# ══════════════════════════════════════════════════════════════════════════════
class TestCheckpointIO(unittest.TestCase):
    """save_checkpoint() and load_checkpoint() round-trip in a temp directory."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.p = patch.object(sc, "CHECKPOINT", Path(self.tmpdir) / "checkpoint.json")
        self.p.start()

    def tearDown(self):
        self.p.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_returns_correct_defaults_when_no_file_exists(self):
        cp = sc.load_checkpoint()
        self.assertEqual(cp["current_year"],       sc.API_YEARS[0])
        self.assertEqual(cp["batch_idx"],          0)
        self.assertEqual(cp["page"],               0)
        self.assertEqual(cp["completed_years"],    [])
        self.assertEqual(cp["requests_in_window"], 0)
        self.assertIsNone(cp["window_start"])
        self.assertIsNone(cp["next_run_after"])
        self.assertFalse(cp["cron_installed"])

    def test_save_and_reload_preserves_all_fields(self):
        cp = sc.load_checkpoint()
        cp.update({
            "current_year":       "2005",
            "batch_idx":          7,
            "page":               23,
            "completed_years":    ["1996", "1997", "1998"],
            "requests_in_window": 847,
        })
        sc.save_checkpoint(cp)
        reloaded = sc.load_checkpoint()
        self.assertEqual(reloaded["current_year"],       "2005")
        self.assertEqual(reloaded["batch_idx"],          7)
        self.assertEqual(reloaded["page"],               23)
        self.assertEqual(reloaded["completed_years"],    ["1996", "1997", "1998"])
        self.assertEqual(reloaded["requests_in_window"], 847)

    def test_saved_file_is_valid_json(self):
        sc.save_checkpoint(sc.load_checkpoint())
        with open(str(Path(self.tmpdir) / "checkpoint.json")) as fh:
            self.assertIsInstance(json.load(fh), dict)

    def test_save_overwrites_previous_values(self):
        cp = sc.load_checkpoint()
        cp["batch_idx"] = 3
        sc.save_checkpoint(cp)
        cp["batch_idx"] = 9
        sc.save_checkpoint(cp)
        self.assertEqual(sc.load_checkpoint()["batch_idx"], 9)


# ══════════════════════════════════════════════════════════════════════════════
class TestYearDataIO(unittest.TestCase):
    """save_year_data() and load_year_data() in a temp directory."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.p = patch.object(sc, "TEMP_DIR", Path(self.tmpdir))
        self.p.start()

    def tearDown(self):
        self.p.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_load_missing_year_returns_empty_dict(self):
        self.assertEqual(sc.load_year_data("1996"), {})

    def test_save_and_reload_roundtrip(self):
        data = {
            "100001": {"id": 100001, "school.name": "MIT",
                       "2010.cost.tuition.in_state": 12000},
            "100002": {"id": 100002, "school.name": "Harvard",
                       "2010.cost.tuition.in_state": 15000},
        }
        sc.save_year_data("2010", data)
        self.assertEqual(sc.load_year_data("2010"), data)

    def test_file_is_named_by_year(self):
        sc.save_year_data("2015", {"x": 1})
        self.assertTrue((Path(self.tmpdir) / "2015_data.json").exists())

    def test_different_years_stored_independently(self):
        sc.save_year_data("1996", {"a": "first"})
        sc.save_year_data("1997", {"b": "second"})
        self.assertEqual(sc.load_year_data("1996"), {"a": "first"})
        self.assertEqual(sc.load_year_data("1997"), {"b": "second"})

    def test_overwrite_replaces_existing_data(self):
        sc.save_year_data("2000", {"k": "original"})
        sc.save_year_data("2000", {"k": "updated"})
        self.assertEqual(sc.load_year_data("2000")["k"], "updated")


# ══════════════════════════════════════════════════════════════════════════════
class TestRateLimitLogic(unittest.TestCase):
    """is_in_hold_period() and refresh_window_if_needed() logic."""

    def test_hold_period_true_when_timestamp_in_future(self):
        future = (datetime.now() + timedelta(hours=1)).isoformat()
        self.assertTrue(sc.is_in_hold_period({"next_run_after": future}))

    def test_hold_period_false_when_timestamp_in_past(self):
        past = (datetime.now() - timedelta(minutes=5)).isoformat()
        self.assertFalse(sc.is_in_hold_period({"next_run_after": past}))

    def test_hold_period_false_when_none(self):
        self.assertFalse(sc.is_in_hold_period({"next_run_after": None}))
        self.assertFalse(sc.is_in_hold_period({}))

    def test_refresh_resets_counter_after_one_hour(self):
        old = (datetime.now() - timedelta(hours=2)).isoformat()
        cp  = {"window_start": old, "requests_in_window": 900, "next_run_after": "x"}
        result = sc.refresh_window_if_needed(cp)
        self.assertEqual(result["requests_in_window"], 0)
        self.assertIsNone(result["next_run_after"])
        # window_start should now be close to now
        new_ws = sc._parse_iso(result["window_start"])
        self.assertLess((datetime.now() - new_ws).total_seconds(), 5)

    def test_refresh_preserves_counter_within_window(self):
        recent = (datetime.now() - timedelta(minutes=30)).isoformat()
        cp     = {"window_start": recent, "requests_in_window": 400, "next_run_after": None}
        result = sc.refresh_window_if_needed(cp)
        self.assertEqual(result["requests_in_window"], 400)
        self.assertEqual(result["window_start"], recent)

    def test_refresh_resets_when_window_start_is_none(self):
        cp = {"window_start": None, "requests_in_window": 500, "next_run_after": "x"}
        self.assertEqual(sc.refresh_window_if_needed(cp)["requests_in_window"], 0)


# ══════════════════════════════════════════════════════════════════════════════
class TestWriteYearCSV(unittest.TestCase):
    """write_year_csv() produces correctly formatted CSV files."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.p = patch.object(sc, "OUTPUT_DIR", Path(self.tmpdir))
        self.p.start()

    def tearDown(self):
        self.p.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _sample_data(self, n=5):
        data = {}
        for i in range(n):
            uid = str(100000 + i)
            data[uid] = {
                "id": int(uid),
                "school.name": "School {}".format(i),
                "2005.admissions.admission_rate.overall": round(0.5 + i * 0.05, 2),
                "2005.cost.tuition.in_state": 10000 + i * 500,
            }
        return data

    def _read_csv(self, year):
        path = Path(self.tmpdir) / "scorecard_{}_{}.csv".format(year, int(year) + 1)
        with open(str(path), encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def _headers(self, year):
        path = Path(self.tmpdir) / "scorecard_{}_{}.csv".format(year, int(year) + 1)
        with open(str(path), encoding="utf-8") as fh:
            return csv.DictReader(fh).fieldnames

    def test_creates_file_with_correct_name(self):
        sc.write_year_csv("2005", self._sample_data())
        self.assertTrue((Path(self.tmpdir) / "scorecard_2005_2006.csv").exists())

    def test_filename_increments_year_by_one(self):
        sc.write_year_csv("1999", self._sample_data(2))
        self.assertTrue((Path(self.tmpdir) / "scorecard_1999_2000.csv").exists())

    def test_id_is_always_first_column(self):
        sc.write_year_csv("2005", self._sample_data())
        self.assertEqual(self._headers("2005")[0], "id")

    def test_correct_row_count(self):
        n = 8
        sc.write_year_csv("2005", self._sample_data(n))
        self.assertEqual(len(self._read_csv("2005")), n)

    def test_all_expected_columns_present(self):
        sc.write_year_csv("2005", self._sample_data(3))
        cols = set(self._headers("2005"))
        for expected in ("id", "school.name",
                         "2005.admissions.admission_rate.overall",
                         "2005.cost.tuition.in_state"):
            self.assertIn(expected, cols)

    def test_data_values_written_correctly(self):
        data = {"123": {"id": 123, "school.name": "Test U",
                        "2010.cost.tuition.in_state": 9999}}
        sc.write_year_csv("2010", data)
        rows = self._read_csv("2010")
        self.assertEqual(rows[0]["school.name"], "Test U")
        self.assertEqual(rows[0]["2010.cost.tuition.in_state"], "9999")

    def test_different_years_produce_separate_files(self):
        sc.write_year_csv("2010", self._sample_data(2))
        sc.write_year_csv("2011", self._sample_data(3))
        self.assertEqual(len(self._read_csv("2010")), 2)
        self.assertEqual(len(self._read_csv("2011")), 3)

    def test_empty_data_does_not_create_file(self):
        sc.write_year_csv("2005", {})
        self.assertFalse((Path(self.tmpdir) / "scorecard_2005_2006.csv").exists())


# ══════════════════════════════════════════════════════════════════════════════
class TestFetchPage(unittest.TestCase):
    """fetch_page() with mocked HTTP responses."""

    def _mock_resp(self, results, total, status=200):
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = {
            "metadata": {"total": total, "page": 0, "per_page": sc.PER_PAGE},
            "results": results,
        }
        if status == 200:
            resp.raise_for_status.return_value = None
        else:
            resp.raise_for_status.side_effect = Exception("HTTP {}".format(status))
        return resp

    @patch("scorecard_pull.requests.get")
    def test_returns_records_and_total_on_success(self, mock_get):
        fake = [{"id": 1, "school.name": "MIT"}, {"id": 2, "school.name": "Harvard"}]
        mock_get.return_value = self._mock_resp(fake, total=500)
        records, total = sc.fetch_page(["id", "school.name"], page=0)
        self.assertEqual(records, fake)
        self.assertEqual(total, 500)

    @patch("scorecard_pull.requests.get")
    def test_sends_correct_query_params(self, mock_get):
        mock_get.return_value = self._mock_resp([], total=0)
        sc.fetch_page(["id", "school.name", "2010.cost.tuition.in_state"], page=5)
        _, kwargs = mock_get.call_args
        params = kwargs["params"]
        self.assertIn("id",                          params["fields"])
        self.assertIn("2010.cost.tuition.in_state",  params["fields"])
        self.assertEqual(params["page"],     5)
        self.assertEqual(params["per_page"], sc.PER_PAGE)
        self.assertEqual(params["api_key"],  sc.API_KEY)

    @patch("scorecard_pull.time.sleep")
    @patch("scorecard_pull.requests.get")
    def test_sleeps_and_retries_after_429(self, mock_get, mock_sleep):
        rate_limited = MagicMock()
        rate_limited.status_code = 429
        rate_limited.raise_for_status.return_value = None
        success = self._mock_resp([{"id": 1}], total=1)
        mock_get.side_effect = [rate_limited, success]
        records, _ = sc.fetch_page(["id"], page=0)
        mock_sleep.assert_called_once_with(60)
        self.assertEqual(len(records), 1)

    @patch("scorecard_pull.time.sleep")
    @patch("scorecard_pull.requests.get")
    def test_retries_on_network_error_then_succeeds(self, mock_get, mock_sleep):
        success = self._mock_resp([{"id": 99}], total=1)
        mock_get.side_effect = [ConnectionError("timeout"), success]
        records, _ = sc.fetch_page(["id"], page=0)
        self.assertEqual(records[0]["id"], 99)

    @patch("scorecard_pull.time.sleep")
    @patch("scorecard_pull.requests.get")
    def test_raises_runtime_error_after_all_retries_fail(self, mock_get, mock_sleep):
        mock_get.side_effect = Exception("always fails")
        with self.assertRaises(RuntimeError):
            sc.fetch_page(["id"], page=0)
        self.assertEqual(mock_get.call_count, sc.RETRY_ATTEMPTS)


# ══════════════════════════════════════════════════════════════════════════════
class TestMainFlow(unittest.TestCase):
    """
    Integration tests for main().

    All API calls are mocked. All file I/O is redirected to a temp directory.
    Two academic years are used (2010, 2011) with small batches and pages
    so tests run quickly.

    Config:
        2 years × 3 batches/year (1 static + 2 year-field batches)
        × 2 pages/batch (total=6 institutions, per_page=3) = 12 fetch calls total
    """

    YEARS        = ["2010", "2011"]
    YEAR_FIELDS  = [
        "admissions.admission_rate.overall",
        "admissions.sat_scores.25th_percentile.critical_reading",
        "cost.tuition.in_state",
        "completion.rate_suppressed.four_year",
    ]
    STATIC_FIELDS = ["id", "school.name"]
    TOTAL_INSTS   = 6
    PER_PAGE_TEST = 3    # → 2 pages per batch

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        tmp = Path(self.tmpdir)
        self.tmp = tmp

        # Redirect all file I/O to temp directory and shrink constants for speed
        patch.object(sc, "OUTPUT_DIR",          tmp / "output").start()
        patch.object(sc, "TEMP_DIR",            tmp / "temp").start()
        patch.object(sc, "CHECKPOINT",          tmp / "checkpoint.json").start()
        patch.object(sc, "LOG_FILE",            tmp / "test.log").start()
        patch.object(sc, "API_KEY",             "fake_key_for_tests").start()
        patch.object(sc, "API_YEARS",           self.YEARS).start()
        patch.object(sc, "FIELD_BATCH_SIZE",    2).start()
        patch.object(sc, "PER_PAGE",            self.PER_PAGE_TEST).start()
        patch.object(sc, "MAX_REQUESTS_PER_HOUR", 9999).start()

        self.mock_setup  = patch("scorecard_pull.setup_scheduler").start()
        self.mock_remove = patch("scorecard_pull.remove_scheduler").start()

        patch.object(sc, "load_data_dictionary",
                     return_value=(self.YEAR_FIELDS, self.STATIC_FIELDS)).start()

    def tearDown(self):
        patch.stopall()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _fake_fetch(self, fields, page):
        return _fake_records(fields, page,
                             total=self.TOTAL_INSTS,
                             per_page=self.PER_PAGE_TEST)

    def _run_main(self, fetch_fn=None):
        fn = fetch_fn or self._fake_fetch
        with patch("scorecard_pull.fetch_page", side_effect=fn):
            sc.main()

    def _read_csv(self, year):
        path = (self.tmp / "output" /
                "scorecard_{}_{}.csv".format(year, int(year) + 1))
        with open(str(path), encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def _csv_headers(self, year):
        path = (self.tmp / "output" /
                "scorecard_{}_{}.csv".format(year, int(year) + 1))
        with open(str(path), encoding="utf-8") as fh:
            return csv.DictReader(fh).fieldnames

    # ── API key guard ─────────────────────────────────────────────────────────

    def test_exits_if_api_key_not_configured(self):
        # main() now returns early instead of calling sys.exit(), so it must
        # complete without error AND produce no output files.
        with patch.object(sc, "API_KEY", "YOUR_API_KEY_HERE"):
            sc.main()   # should return cleanly, not raise
        output = list((self.tmp / "output").glob("*.csv")) if (self.tmp / "output").exists() else []
        self.assertEqual(output, [],
                         msg="main() should produce no CSVs when API key is unconfigured")

    # ── Scheduler lifecycle ───────────────────────────────────────────────────

    def test_scheduler_installed_on_first_run(self):
        self._run_main()
        self.mock_setup.assert_called_once()

    def test_scheduler_not_reinstalled_when_checkpoint_says_installed(self):
        cp = sc.load_checkpoint()
        cp["cron_installed"]   = True
        cp["completed_years"]  = ["2010"]   # pretend first year is done
        cp["current_year"]     = "2011"
        sc.save_checkpoint(cp)
        self._run_main()
        self.mock_setup.assert_not_called()

    def test_scheduler_removed_when_all_years_complete(self):
        self._run_main()
        self.mock_remove.assert_called_once()

    def test_checkpoint_deleted_when_all_years_complete(self):
        self._run_main()
        self.assertFalse((self.tmp / "checkpoint.json").exists())

    # ── Full run output ───────────────────────────────────────────────────────

    def test_produces_one_csv_per_year(self):
        self._run_main()
        for year in self.YEARS:
            fname = "scorecard_{}_{}.csv".format(year, int(year) + 1)
            self.assertTrue((self.tmp / "output" / fname).exists(),
                            msg="Missing CSV for year {}".format(year))

    def test_each_csv_has_correct_institution_count(self):
        self._run_main()
        for year in self.YEARS:
            rows = self._read_csv(year)
            self.assertEqual(len(rows), self.TOTAL_INSTS,
                             msg="Wrong row count for year {}".format(year))

    def test_id_is_first_column_in_all_csvs(self):
        self._run_main()
        for year in self.YEARS:
            self.assertEqual(self._csv_headers(year)[0], "id",
                             msg="'id' not first in year {}".format(year))

    def test_csvs_contain_year_prefixed_columns(self):
        self._run_main()
        for year in self.YEARS:
            cols = set(self._csv_headers(year))
            year_cols = [c for c in cols if c[:4].isdigit()]
            self.assertTrue(len(year_cols) > 0,
                            msg="No year-prefixed columns in year {}".format(year))
            self.assertTrue(
                all(c.startswith("{}.".format(year)) for c in year_cols),
                msg="Wrong year prefix in CSV for year {}".format(year)
            )

    def test_paginated_records_all_collected(self):
        """
        With 6 institutions across 2 pages, all 6 must appear in the CSV
        (tests that the pagination loop actually iterates multiple pages).
        """
        self._run_main()
        rows = self._read_csv("2010")
        ids  = {int(r["id"]) for r in rows}
        # IDs 100000–100005 should all be present
        self.assertEqual(ids, {100000 + i for i in range(self.TOTAL_INSTS)})

    # ── Rate-limit handling ───────────────────────────────────────────────────

    def test_rate_limit_saves_checkpoint_and_stops(self):
        """When the request budget is exhausted mid-run, state is persisted."""
        with patch.object(sc, "MAX_REQUESTS_PER_HOUR", 3):
            self._run_main()

        self.assertTrue((self.tmp / "checkpoint.json").exists())
        cp = sc.load_checkpoint()
        self.assertIsNotNone(cp.get("next_run_after"),
                             msg="hold period should be set after rate limit")
        self.assertTrue(sc.is_in_hold_period(cp),
                        msg="hold period should be in the future")

    def test_hold_period_prevents_any_api_calls(self):
        """If a hold period is active, main() exits immediately without fetching."""
        cp = sc.load_checkpoint()
        cp["next_run_after"] = (datetime.now() + timedelta(hours=1)).isoformat()
        sc.save_checkpoint(cp)

        call_count = [0]
        def counting_fetch(fields, page):
            call_count[0] += 1
            return self._fake_fetch(fields, page)

        self._run_main(fetch_fn=counting_fetch)
        self.assertEqual(call_count[0], 0,
                         msg="API should not be called during a hold period")

    def test_rate_limit_partial_year_data_saved_to_temp(self):
        """When rate-limited mid-year, partial institution data is saved."""
        with patch.object(sc, "MAX_REQUESTS_PER_HOUR", 3):
            self._run_main()

        cp = sc.load_checkpoint()
        year = cp.get("current_year", "2010")
        temp_file = self.tmp / "temp" / "{}_data.json".format(year)
        self.assertTrue(temp_file.exists(),
                        msg="Partial year data not saved to temp/")

    # ── Resume from checkpoint ────────────────────────────────────────────────

    def test_skips_already_completed_years(self):
        """Years listed in completed_years must not be re-fetched or re-written."""
        cp = sc.load_checkpoint()
        cp["cron_installed"]  = True
        cp["completed_years"] = ["2010"]
        cp["current_year"]    = "2011"
        sc.save_checkpoint(cp)

        fetched_pages = []
        def recording_fetch(fields, page):
            fetched_pages.append((fields, page))
            return self._fake_fetch(fields, page)

        self._run_main(fetch_fn=recording_fetch)

        # Only 2011 CSV should exist; 2010 was not re-fetched
        self.assertFalse(
            (self.tmp / "output" / "scorecard_2010_2011.csv").exists(),
            msg="2010 CSV should not be written on a resume run that skips it"
        )
        self.assertTrue(
            (self.tmp / "output" / "scorecard_2011_2012.csv").exists()
        )

    def test_resumes_at_correct_batch_and_page(self):
        """
        Checkpoint at batch_idx=1, page=1 should cause the first fetch
        to be for page 1 of batch 1 (not page 0 of batch 0).
        """
        cp = sc.load_checkpoint()
        cp["cron_installed"] = True
        cp["current_year"]   = "2010"
        cp["batch_idx"]      = 1
        cp["page"]           = 1
        sc.save_checkpoint(cp)

        # Pre-populate partial data so the year isn't starting fresh
        sc.save_year_data("2010", {
            str(100000 + i): {"id": 100000 + i, "school.name": "School"}
            for i in range(3)
        })

        pages_fetched = []
        def recording_fetch(fields, page):
            pages_fetched.append(page)
            return self._fake_fetch(fields, page)

        self._run_main(fetch_fn=recording_fetch)

        self.assertGreater(len(pages_fetched), 0)
        self.assertEqual(
            pages_fetched[0], 1,
            msg="Expected resume at page 1, but first fetch was page {}".format(
                pages_fetched[0])
        )

    def test_completed_years_accumulate_in_checkpoint(self):
        """After a full run, checkpoint completed_years contains all years."""
        self._run_main()
        # Checkpoint is deleted on success, so we verify via the CSVs
        for year in self.YEARS:
            fname = "scorecard_{}_{}.csv".format(year, int(year) + 1)
            self.assertTrue((self.tmp / "output" / fname).exists())


# ══════════════════════════════════════════════════════════════════════════════
class TestScheduler(unittest.TestCase):
    """
    Cross-platform scheduler: _setup_crontab / _remove_crontab (Mac/Linux)
    and _setup_task_scheduler / _remove_task_scheduler (Windows).
    subprocess.run is mocked so nothing is actually installed.
    """

    SCRIPT_PATH = str(Path(sc.__file__).resolve())

    def setUp(self):
        # Silence log() so it doesn't write to disk during scheduler tests
        patch("scorecard_pull.log").start()

    def tearDown(self):
        patch.stopall()

    # ── Mac / Linux crontab ───────────────────────────────────────────────────

    @patch("scorecard_pull.subprocess.run")
    def test_crontab_installs_correct_entry(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        sc._setup_crontab()
        # The second subprocess call writes the new crontab
        write_call   = mock_run.call_args_list[-1]
        written      = write_call[1]["input"]
        self.assertIn(self.SCRIPT_PATH, written)
        self.assertIn("*/{} * * * *".format(sc.CRON_INTERVAL_MIN), written)

    @patch("scorecard_pull.subprocess.run")
    def test_crontab_not_duplicated_when_already_present(self, mock_run):
        existing = "*/{} * * * * python3 {} >> /log 2>&1\n".format(
            sc.CRON_INTERVAL_MIN, self.SCRIPT_PATH)
        mock_run.return_value = MagicMock(returncode=0, stdout=existing)
        sc._setup_crontab()
        # Should only call `crontab -l` (1 call), never `crontab -` (write)
        self.assertEqual(mock_run.call_count, 1,
                         msg="Should not write crontab when entry already present")

    @patch("scorecard_pull.subprocess.run")
    def test_crontab_removes_script_line_only(self, mock_run):
        other_job = "0 9 * * * /usr/bin/other_job\n"
        our_entry = "*/{} * * * * python3 {} >> /log 2>&1\n".format(
            sc.CRON_INTERVAL_MIN, self.SCRIPT_PATH)
        mock_run.return_value = MagicMock(returncode=0, stdout=other_job + our_entry)
        sc._remove_crontab()
        write_call = mock_run.call_args_list[-1]
        written    = write_call[1]["input"]
        self.assertIn("other_job",        written)
        self.assertNotIn("scorecard_pull", written)

    @patch("scorecard_pull.subprocess.run")
    def test_crontab_remove_handles_missing_crontab_gracefully(self, mock_run):
        # Non-zero return code means crontab doesn't exist yet
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        sc._remove_crontab()   # should not raise

    # ── Windows Task Scheduler ────────────────────────────────────────────────

    @patch("scorecard_pull.subprocess.run")
    def test_task_scheduler_create_uses_schtasks(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        sc._setup_task_scheduler()
        args = mock_run.call_args[0][0]
        self.assertIn("schtasks",       args)
        self.assertIn("/create",        args)
        self.assertIn(sc.TASK_NAME,     args)
        self.assertIn("/sc",            args)
        self.assertIn("minute",         args)

    @patch("scorecard_pull.subprocess.run")
    def test_task_scheduler_delete_uses_schtasks(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        sc._remove_task_scheduler()
        args = mock_run.call_args[0][0]
        self.assertIn("schtasks",   args)
        self.assertIn("/delete",    args)
        self.assertIn(sc.TASK_NAME, args)
        self.assertIn("/f",         args)

    @patch("scorecard_pull.subprocess.run")
    def test_task_scheduler_create_failure_does_not_raise(self, mock_run):
        mock_run.side_effect = Exception("schtasks not found")
        sc._setup_task_scheduler()   # should warn but not crash

    # ── setup_scheduler / remove_scheduler routing ────────────────────────────

    @patch("scorecard_pull._setup_crontab")
    @patch("scorecard_pull._setup_task_scheduler")
    def test_setup_scheduler_calls_crontab_on_non_windows(self, mock_win, mock_mac):
        with patch.object(sc, "IS_WINDOWS", False):
            sc.setup_scheduler()
        mock_mac.assert_called_once()
        mock_win.assert_not_called()

    @patch("scorecard_pull._setup_crontab")
    @patch("scorecard_pull._setup_task_scheduler")
    def test_setup_scheduler_calls_task_scheduler_on_windows(self, mock_win, mock_mac):
        with patch.object(sc, "IS_WINDOWS", True):
            sc.setup_scheduler()
        mock_win.assert_called_once()
        mock_mac.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# Live API key resolution — used by TestLiveAPI below.
# Priority: SCORECARD_API_KEY env var > API_KEY constant in scorecard_pull.py
_LIVE_API_KEY = os.environ.get("SCORECARD_API_KEY") or (
    sc.API_KEY if sc.API_KEY != "YOUR_API_KEY_HERE" else None
)


@unittest.skipIf(
    _LIVE_API_KEY is None,
    "Live API tests skipped - set API_KEY in scorecard_pull.py or "
    "export SCORECARD_API_KEY=<your_key> to enable them."
)
class TestLiveAPI(unittest.TestCase):
    """
    Integration tests that make REAL HTTP requests to the College Scorecard API.

    What these verify:
      - Network connectivity to the API endpoint
      - That the key is valid and accepted
      - That static fields (school.name, school.state) come back in responses
      - That year-prefixed fields (e.g. 2020.admissions.admission_rate.overall)
        are accepted and returned as response keys
      - That pagination advances correctly and returns non-overlapping record sets
      - That a real sample from the data dictionary CSV is accepted by the API

    These tests are SKIPPED automatically when no API key is configured.
    To enable them, either:
      1. Edit API_KEY in scorecard_pull.py with your real key, or
      2. export SCORECARD_API_KEY=<your_key> before running the tests

    ~8–10 real API requests are made total — well within the hourly limit.
    """

    @classmethod
    def setUpClass(cls):
        # Patch sc.API_KEY so fetch_page picks up the live key (handles the case
        # where the env var overrides the module constant).
        cls._key_patch = patch.object(sc, "API_KEY", _LIVE_API_KEY)
        cls._key_patch.start()
        # Load the real data dictionary once for all live tests.
        cls.year_fields, cls.static_fields = sc.load_data_dictionary()

    @classmethod
    def tearDownClass(cls):
        cls._key_patch.stop()

    def setUp(self):
        # Suppress log() so live tests don't write into the real log file.
        self._log_patch = patch("scorecard_pull.log")
        self._log_patch.start()

    def tearDown(self):
        self._log_patch.stop()

    # ── Connectivity ──────────────────────────────────────────────────────────

    def test_api_returns_records(self):
        """fetch_page() with a live key returns a non-empty list of records."""
        records, total = sc.fetch_page(["id"], page=0)
        self.assertIsInstance(records, list)
        self.assertGreater(
            len(records), 0,
            msg="API returned 0 records — check connectivity and API key validity"
        )

    def test_total_institution_count_is_plausible(self):
        """Total reported by the API should be in the realistic 5,000-15,000 range."""
        _, total = sc.fetch_page(["id"], page=0)
        self.assertGreater(total, 5000,
                           msg="total={} is unexpectedly low".format(total))
        self.assertLess(total, 15000,
                        msg="total={} is unexpectedly high".format(total))

    def test_records_contain_id_field(self):
        """Every returned record must include the 'id' field when it is requested."""
        records, _ = sc.fetch_page(["id", "school.name"], page=0)
        for i, rec in enumerate(records):
            self.assertIn(
                "id", rec,
                msg="Record {} is missing 'id'. Keys present: {}".format(
                    i, list(rec.keys()))
            )

    # ── Static fields ─────────────────────────────────────────────────────────

    def test_static_school_fields_returned(self):
        """Requesting school.name / school.state returns those keys for real records."""
        records, _ = sc.fetch_page(
            ["id", "school.name", "school.city", "school.state"], page=0
        )
        has_name  = any(rec.get("school.name")  for rec in records)
        has_state = any(rec.get("school.state") for rec in records)
        self.assertTrue(has_name,
                        msg="No records had a non-null school.name in the response")
        self.assertTrue(has_state,
                        msg="No records had a non-null school.state in the response")

    # ── Year-prefixed fields ──────────────────────────────────────────────────

    def test_year_prefixed_field_returned_in_response(self):
        """
        Requesting a year-prefixed field (2020.admissions.admission_rate.overall)
        should not error and the field should appear as a key in at least one record.
        """
        prefixed = "2020.admissions.admission_rate.overall"
        records, total = sc.fetch_page(["id", prefixed], page=0)
        self.assertGreater(total, 0,
                           msg="API returned 0 total for year-prefixed request")
        all_keys = {k for rec in records for k in rec.keys()}
        self.assertIn(
            prefixed, all_keys,
            msg="Field '{}' not in any record. Keys seen: {}".format(
                prefixed, sorted(all_keys))
        )

    def test_multiple_categories_in_one_request(self):
        """A single request mixing admissions, cost, and student fields is accepted."""
        year   = "2015"
        fields = ["id"] + [
            "{}.{}".format(year, f) for f in [
                "admissions.admission_rate.overall",
                "cost.tuition.in_state",
                "student.size",
            ]
        ]
        records, total = sc.fetch_page(fields, page=0)
        self.assertGreater(len(records), 0,
                           msg="Multi-category request returned 0 records")

    def test_data_dictionary_sample_fields_accepted(self):
        """
        The first 10 year-specific field names from the real data dictionary CSV,
        prefixed with '2020.', should be accepted by the API without error.
        """
        year   = "2020"
        sample = ["id"] + ["{}.{}".format(year, f) for f in self.year_fields[:10]]
        records, total = sc.fetch_page(sample, page=0)
        self.assertGreater(
            total, 0,
            msg="API rejected a sample of real data dictionary fields: {}".format(
                sample[1:])
        )

    def test_older_year_fields_accepted(self):
        """Year-prefixed fields for an older year (2000) are accepted by the API."""
        year    = "2000"
        field   = "admissions.admission_rate.overall"
        prefixed = "{}.{}".format(year, field)
        records, total = sc.fetch_page(["id", prefixed], page=0)
        self.assertGreater(total, 0,
                           msg="API returned 0 total for older year '{}'".format(year))

    # ── Pagination ────────────────────────────────────────────────────────────

    def test_per_page_count_does_not_exceed_limit(self):
        """The API should never return more records per page than PER_PAGE."""
        records, _ = sc.fetch_page(["id"], page=0)
        self.assertLessEqual(
            len(records), sc.PER_PAGE,
            msg="Got {} records, expected <= {}".format(len(records), sc.PER_PAGE)
        )

    def test_pagination_returns_distinct_record_sets(self):
        """
        Pages 0 and 1 must return completely non-overlapping institution IDs —
        confirms the pagination offset is advancing correctly.
        """
        recs0, total = sc.fetch_page(["id"], page=0)
        self.assertGreater(
            total, sc.PER_PAGE,
            msg="Too few total records ({}) to test pagination".format(total)
        )
        recs1, _ = sc.fetch_page(["id"], page=1)
        ids0    = {r["id"] for r in recs0}
        ids1    = {r["id"] for r in recs1}
        overlap = ids0 & ids1
        self.assertEqual(
            len(overlap), 0,
            msg="Pages 0 and 1 share institution IDs: {}".format(overlap)
        )

    def test_three_sequential_pages_cover_distinct_records(self):
        """
        Fetching pages 0, 1, and 2 should collectively yield at least
        PER_PAGE * 3 unique institution IDs.
        """
        seen_ids = set()
        for page in range(3):
            records, _ = sc.fetch_page(["id"], page=page)
            for r in records:
                seen_ids.add(r["id"])
        self.assertGreaterEqual(
            len(seen_ids), sc.PER_PAGE * 3,
            msg="Expected >= {} unique IDs across 3 pages, got {}".format(
                sc.PER_PAGE * 3, len(seen_ids))
        )


# ══════════════════════════════════════════════════════════════════════════════
def run_tests(verbosity=2):
    """
    Run the full test suite and return the result object.

    Use this from a Jupyter cell instead of running the file directly:

        import test_scorecard_pull
        result = test_scorecard_pull.run_tests()

    Or after %run:

        %run test_scorecard_pull.py  # auto-calls run_tests() for you

    Unlike unittest.main(), this function does NOT call sys.exit(), so it
    works cleanly inside a Jupyter kernel without killing the session.
    """
    import io
    # Wrap stdout in a UTF-8 stream so skip/error messages with non-ASCII
    # characters don't crash on Python 3.6's default ASCII terminal (Windows).
    try:
        stream = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  line_buffering=True)
    except AttributeError:
        stream = sys.stdout   # Jupyter notebooks expose a plain StringIO
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=verbosity, stream=stream)
    return runner.run(suite)


if __name__ == "__main__":
    run_tests()
