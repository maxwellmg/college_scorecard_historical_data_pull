#!/usr/bin/env python3
"""
College Scorecard Historical Data Downloader

Downloads all available fields for every academic year (1996-97 through 2025-26)
from the College Scorecard API. Handles rate limits automatically using crontab,
and resumes from a checkpoint if the laptop restarts or the script exits early.

Estimated run time: ~92 hours of active pulling (~5 days calendar time).
The script is fully hands-off after the first run — just leave the laptop on.

Quick start:
  1. pip install requests
  2. Set API_KEY below (free at https://api.data.gov/signup)
  3. python3 scorecard_pull.py
  4. Walk away — crontab handles restarts. CSVs land in output/

Output: one CSV per year named  scorecard_1996_1997.csv, scorecard_1997_1998.csv, ...
"""

from __future__ import print_function  # py2 compat guard (harmless on py3)

import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

# ── USER CONFIGURATION ─────────────────────────────────────────────────────
API_KEY = "YOUR_API_KEY_HERE"   # <-- paste your api.data.gov key here


def _parse_iso(s):
    """Parse an isoformat datetime string (Python 3.6-compatible)."""
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    raise ValueError("Cannot parse datetime string: {}".format(s))


def _unlink(path):
    """Delete a file if it exists (Path.unlink missing_ok not in Python 3.6)."""
    if path.exists():
        path.unlink()

# ── TUNING (change only if needed) ─────────────────────────────────────────
BASE_URL            = "https://api.data.gov/ed/collegescorecard/v1/schools.json"
PER_PAGE            = 100   # max records per page the API allows
MAX_REQUESTS_PER_HOUR = 950 # stay under the 1000/hr hard limit
FIELD_BATCH_SIZE    = 75    # year-specific fields per request (URL length safety)
CRON_INTERVAL_MIN   = 15   # how often crontab wakes the script to check for work
RETRY_ATTEMPTS      = 3
RETRY_DELAY_SEC     = 10

# ── PATHS ───────────────────────────────────────────────────────────────────
# __file__ is defined when running as a script or via `import`, but not when
# the code is exec()'d or pasted into a Jupyter cell.  The fallback uses the
# current working directory so Jupyter users can place the notebook alongside
# the script and API_Documentation folder.
try:
    SCRIPT_DIR  = Path(__file__).parent.resolve()
    _SCRIPT_FILE = Path(__file__).resolve()
except NameError:
    SCRIPT_DIR  = Path.cwd()
    _SCRIPT_FILE = SCRIPT_DIR / "scorecard_pull.py"

OUTPUT_DIR  = SCRIPT_DIR / "output"
TEMP_DIR    = SCRIPT_DIR / "temp"
CHECKPOINT  = SCRIPT_DIR / "checkpoint.json"
DATA_DICT   = SCRIPT_DIR / "API_Documentation" / "CollegeScorecardDataDictionary.csv"
LOG_FILE    = SCRIPT_DIR / "scorecard_pull.log"

# ── CONSTANTS ───────────────────────────────────────────────────────────────
YEAR_CATS   = {"academics", "admissions", "aid", "completion",
               "cost", "earnings", "repayment", "student"}
STATIC_CATS = {"root", "school"}
API_YEARS   = [str(y) for y in range(1996, 2026)]   # 30 academic years


# ───────────────────────────── Logging ──────────────────────────────────────

def log(msg):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = "[{}] {}".format(ts, msg)
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        # Fallback for terminals that don't support non-ASCII (e.g. Windows cmd)
        print(line.encode("ascii", "replace").decode("ascii"), flush=True)
    with open(str(LOG_FILE), "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


# ───────────────────────── Data Dictionary ──────────────────────────────────

def load_data_dictionary():
    """
    Parse CollegeScorecardDataDictionary.csv.

    Returns
    -------
    year_fields   : list of field names WITHOUT year prefix
                    e.g. "admissions.admission_rate.overall"
    static_fields : list of full API field names that need no year prefix
                    e.g. "id", "school.name"
    """
    year_fields   = []
    static_fields = []
    seen_year     = set()
    seen_static   = set()

    with open(str(DATA_DICT), encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            cat  = (row.get("dev-category") or "").strip()
            name = (row.get("developer-friendly name") or "").strip()
            if not name:
                continue

            if cat in YEAR_CATS:
                field = "{}.{}".format(cat, name)
                if field not in seen_year:
                    year_fields.append(field)
                    seen_year.add(field)

            elif cat in STATIC_CATS:
                # root fields (e.g. "id") have no prefix; school fields get "school."
                field = name if cat == "root" else "school.{}".format(name)
                if field not in seen_static:
                    static_fields.append(field)
                    seen_static.add(field)

    return year_fields, static_fields


def build_batches(year, year_fields, static_fields):
    """
    Return a list of field lists for a given year.
    Each inner list is the set of API fields to request in one call.
    'id' is always present for joining records across batches.
    """
    # Batch 0: all static (school-level) fields
    static_batch = ["id"] + [f for f in static_fields if f != "id"]

    # Batches 1-N: year-prefixed fields in chunks of FIELD_BATCH_SIZE
    prefixed = ["{}.{}".format(year, f) for f in year_fields]
    batches  = [static_batch]
    for i in range(0, len(prefixed), FIELD_BATCH_SIZE):
        batches.append(["id"] + prefixed[i : i + FIELD_BATCH_SIZE])

    return batches


# ─────────────────────────── Checkpoint I/O ─────────────────────────────────

def _load_json_safe(path, description):
    """
    Read a JSON file and return its contents as a Python object.

    If the file is corrupted (e.g. a write was interrupted mid-stream, leaving
    valid JSON followed by truncated garbage), attempt to recover the first
    complete JSON value via raw_decode so no saved progress is lost.
    Returns None if the file cannot be read at all.
    """
    with open(str(path), encoding="utf-8") as fh:
        content = fh.read()
    try:
        return json.loads(content)
    except ValueError:
        try:
            obj, _ = json.JSONDecoder().raw_decode(content)
            log("WARNING: {} was corrupted (truncated write). "
                "Recovered {} records from the readable portion.".format(
                    description, len(obj) if isinstance(obj, dict) else "?"))
            return obj
        except ValueError:
            log("WARNING: {} is unreadable and cannot be recovered.".format(description))
            return None


def _save_json_atomic(path, data, **dump_kwargs):
    """
    Write data to path as JSON using a write-then-rename pattern so that an
    interrupted write never corrupts the existing file.  The old file remains
    intact until the new one is fully flushed to disk.
    """
    tmp = path.with_suffix(".tmp")
    with open(str(tmp), "w", encoding="utf-8") as fh:
        json.dump(data, fh, **dump_kwargs)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(str(tmp), str(path))   # atomic on both Windows and Unix


def _default_checkpoint():
    return {
        "current_year":        API_YEARS[0],
        "batch_idx":           0,
        "page":                0,
        "completed_years":     [],
        "requests_in_window":  0,
        "window_start":        None,
        "next_run_after":      None,
        "cron_installed":      False,
    }


def load_checkpoint():
    if not CHECKPOINT.exists():
        return _default_checkpoint()
    cp = _load_json_safe(CHECKPOINT, "checkpoint.json")
    if cp is None:
        log("Starting from scratch due to unreadable checkpoint.")
        return _default_checkpoint()
    return cp


def save_checkpoint(cp):
    _save_json_atomic(CHECKPOINT, cp, indent=2)


def load_year_data(year):
    path = TEMP_DIR / "{}_data.json".format(year)
    if not path.exists():
        return {}
    data = _load_json_safe(path, "{}_data.json".format(year))
    return data if data is not None else {}


def save_year_data(year, data):
    TEMP_DIR.mkdir(exist_ok=True)
    path = TEMP_DIR / "{}_data.json".format(year)
    _save_json_atomic(path, data)


# ─────────────────── Cross-platform Scheduler Management ────────────────────

IS_WINDOWS = sys.platform.startswith("win")
TASK_NAME  = "CollegeScorecardPull"   # Windows Task Scheduler task name


def setup_scheduler():
    """
    Install an automatic restart schedule.
    - Mac/Linux: adds a crontab entry
    - Windows:   creates a Task Scheduler task via schtasks
    """
    if IS_WINDOWS:
        _setup_task_scheduler()
    else:
        _setup_crontab()


def remove_scheduler():
    """Remove the automatic restart schedule."""
    if IS_WINDOWS:
        _remove_task_scheduler()
    else:
        _remove_crontab()


# ── Mac / Linux ──────────────────────────────────────────────────────────────

def _setup_crontab():
    script   = str(_SCRIPT_FILE)
    python   = sys.executable
    log_path = str(LOG_FILE)
    cron_cmd = "*/{interval} * * * * {py} {sc} >> {lg} 2>&1".format(
        interval=CRON_INTERVAL_MIN, py=python, sc=script, lg=log_path
    )
    result = subprocess.run(
        ["crontab", "-l"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
    )
    existing = result.stdout if result.returncode == 0 else ""
    if script in existing:
        return
    new_crontab = existing.rstrip("\n") + "\n" + cron_cmd + "\n"
    subprocess.run(
        ["crontab", "-"],
        input=new_crontab,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
        check=True,
    )
    log("Crontab installed: script runs every {} min automatically.".format(CRON_INTERVAL_MIN))


def _remove_crontab():
    marker = str(_SCRIPT_FILE)
    result = subprocess.run(
        ["crontab", "-l"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
    )
    if result.returncode != 0:
        return
    lines = [ln for ln in result.stdout.splitlines(keepends=True) if marker not in ln]
    subprocess.run(
        ["crontab", "-"],
        input="".join(lines),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
    )
    log("Crontab entry removed — collection is complete.")


# ── Windows ──────────────────────────────────────────────────────────────────

def _setup_task_scheduler():
    script = str(_SCRIPT_FILE)
    python = sys.executable
    # Run as the current user; no admin rights required.
    # Note: when launching from Jupyter/conda the task inherits sys.executable
    # (the full path to that Python), so packages installed in the same env
    # are available when the task fires.
    cmd = [
        "schtasks", "/create",
        "/tn", TASK_NAME,
        "/tr", '"{}" "{}"'.format(python, script),
        "/sc", "minute",
        "/mo", str(CRON_INTERVAL_MIN),
        "/f",   # overwrite if task already exists
    ]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
        )
        if result.returncode == 0:
            log("Task Scheduler task '{}' created: runs every {} min.".format(
                TASK_NAME, CRON_INTERVAL_MIN))
            log("  Python: {}".format(python))
            log("  Script: {}".format(script))
            log("  Verify in Task Scheduler or run: schtasks /query /tn {}".format(TASK_NAME))
        else:
            log("WARNING: schtasks returned exit code {}: {}".format(
                result.returncode, result.stderr.strip()))
            log("Auto-resume via Task Scheduler may not work.")
            log("Fallback: re-run sc.main() in Jupyter after each rate-limit pause (~1 hour).")
    except Exception as exc:
        log("WARNING: Could not create Task Scheduler task: {}".format(exc))
        log("Fallback: re-run sc.main() in Jupyter after each rate-limit pause (~1 hour).")


def _remove_task_scheduler():
    try:
        subprocess.run(
            ["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
            check=True,
        )
        log("Task Scheduler task '{}' removed -- collection is complete.".format(TASK_NAME))
    except Exception as exc:
        log("WARNING: Could not remove Task Scheduler task: {}".format(exc))


# ─────────────────────────── Rate Limit Logic ───────────────────────────────

def is_in_hold_period(cp):
    """Return True if we are still waiting for the rate-limit window to reset."""
    nra = cp.get("next_run_after")
    if not nra:
        return False
    return _parse_iso(nra) > datetime.now()


def refresh_window_if_needed(cp):
    """Reset the per-hour request counter once the current window expires."""
    now = datetime.now()
    ws  = cp.get("window_start")
    if ws is None or (now - _parse_iso(ws)) >= timedelta(hours=1):
        cp["window_start"]       = now.isoformat()
        cp["requests_in_window"] = 0
        cp["next_run_after"]     = None
    return cp


# ──────────────────────────── API Fetching ──────────────────────────────────

def fetch_page(fields, page):
    """
    Request one page from the API.
    Returns (list_of_records, total_record_count).
    Raises RuntimeError if all retries fail.
    """
    params = {
        "fields":   ",".join(fields),
        "page":     page,
        "per_page": PER_PAGE,
        "api_key":  API_KEY,
    }
    for attempt in range(RETRY_ATTEMPTS):
        try:
            resp = requests.get(BASE_URL, params=params, timeout=30)
            if resp.status_code == 429:
                log("API returned 429 (rate limited) — sleeping 60 s then retrying")
                time.sleep(60)
                continue
            resp.raise_for_status()
            body  = resp.json()
            total = int(body.get("metadata", {}).get("total", 0))
            return body.get("results", []), total
        except Exception as exc:
            log("  Request error (attempt {}/{}): {}".format(
                attempt + 1, RETRY_ATTEMPTS, exc))
            if attempt < RETRY_ATTEMPTS - 1:
                time.sleep(RETRY_DELAY_SEC)
    raise RuntimeError("API request failed after {} attempts".format(RETRY_ATTEMPTS))


# ──────────────────────────── CSV Output ────────────────────────────────────

def write_year_csv(year, year_data):
    """Write all collected records for one year to a CSV file."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    outfile = OUTPUT_DIR / "scorecard_{}_{}.csv".format(year, int(year) + 1)

    if not year_data:
        log("  No data for year {} — skipping CSV write.".format(year))
        return

    # Build column list preserving insertion order; 'id' always first
    all_cols = []
    seen     = set()
    for record in year_data.values():
        for k in record:
            if k not in seen:
                all_cols.append(k)
                seen.add(k)
    if "id" in all_cols:
        all_cols = ["id"] + [c for c in all_cols if c != "id"]

    with open(str(outfile), "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=all_cols, extrasaction="ignore")
        writer.writeheader()
        for record in year_data.values():
            writer.writerow(record)

    log("  Wrote {}: {:,} institutions, {:,} columns".format(
        outfile.name, len(year_data), len(all_cols)))


# ────────────────────────────── Main ────────────────────────────────────────

def main():
    OUTPUT_DIR.mkdir(exist_ok=True)
    TEMP_DIR.mkdir(exist_ok=True)

    log("=" * 60)
    log("College Scorecard data pull — starting")

    if API_KEY == "YOUR_API_KEY_HERE":
        log("ERROR: API_KEY not configured. "
            "Edit scorecard_pull.py and replace YOUR_API_KEY_HERE with your key.")
        return

    cp = load_checkpoint()

    # If we triggered the rate limit last run, wait for the hold period to pass
    if is_in_hold_period(cp):
        log("Rate-limit hold active until {}. Exiting — cron will resume.".format(
            cp["next_run_after"]))
        return

    # First ever run: install the crontab scheduler
    if not cp["cron_installed"]:
        setup_scheduler()
        cp["cron_installed"] = True
        save_checkpoint(cp)

    # Reset the hourly request counter if the window has expired
    cp = refresh_window_if_needed(cp)

    year_fields, static_fields = load_data_dictionary()
    log("Data dictionary: {} year-specific fields, {} static fields".format(
        len(year_fields), len(static_fields)))

    completed = set(cp["completed_years"])
    remaining = [y for y in API_YEARS if y not in completed]
    n_batches_per_year = max(1, (len(year_fields) + FIELD_BATCH_SIZE - 1) // FIELD_BATCH_SIZE) + 1
    est_total_req = n_batches_per_year * 65 * len(API_YEARS)   # 65 pages is a rough estimate
    log("Progress: {}/{} years done | {} remaining".format(
        len(completed), len(API_YEARS), len(remaining)))
    log("Est. total requests: {:,} | Est. hours at {}/hr: {:.1f}".format(
        est_total_req, MAX_REQUESTS_PER_HOUR, est_total_req / MAX_REQUESTS_PER_HOUR))

    all_done = True   # Will be set False if we exit due to rate limit

    for year in API_YEARS:
        if year in completed:
            continue

        log("--- Year {}-{} ---".format(year, int(year) + 1))
        batches   = build_batches(year, year_fields, static_fields)
        year_data = load_year_data(year)   # resumes partial data if available

        log("  {} field batches (~{} API requests for this year)".format(
            len(batches), len(batches) * 65))  # ~65 pages per institution dataset

        # Where to resume within this year
        resume_batch = cp["batch_idx"] if year == cp["current_year"] else 0
        resume_page  = cp["page"]      if year == cp["current_year"] else 0

        rate_limited = False

        for b_idx in range(resume_batch, len(batches)):
            start_page  = resume_page if b_idx == resume_batch else 0
            page        = start_page
            total_pages = None

            while True:
                # ── Rate limit guard ────────────────────────────────────────
                if cp["requests_in_window"] >= MAX_REQUESTS_PER_HOUR:
                    # Calculate when the current window resets (+ 5 min buffer)
                    hold_until = (
                        _parse_iso(cp["window_start"])
                        + timedelta(hours=1, minutes=5)
                    ).isoformat()
                    cp["current_year"]      = year
                    cp["batch_idx"]         = b_idx
                    cp["page"]              = page
                    cp["next_run_after"]    = hold_until
                    save_checkpoint(cp)
                    save_year_data(year, year_data)
                    log("Rate limit reached ({} requests this window).".format(
                        cp["requests_in_window"]))
                    log("Checkpoint saved. Next run allowed after {}.".format(hold_until))
                    rate_limited = True
                    break

                # ── Fetch page ──────────────────────────────────────────────
                try:
                    results, total = fetch_page(batches[b_idx], page)
                except RuntimeError as exc:
                    log("Fatal fetch error: {}. Saving progress and exiting.".format(exc))
                    save_year_data(year, year_data)
                    cp["current_year"] = year
                    cp["batch_idx"]    = b_idx
                    cp["page"]         = page
                    save_checkpoint(cp)
                    return

                cp["requests_in_window"] += 1

                if total_pages is None:
                    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
                    log("  Batch {}/{}: {:,} institutions across {} pages".format(
                        b_idx + 1, len(batches), total, total_pages))

                # Merge records into year_data dict keyed by institution id
                for record in results:
                    uid = str(record.get("id", "")).strip()
                    if uid:
                        if uid not in year_data:
                            year_data[uid] = {}
                        year_data[uid].update(record)

                log("    page {}/{} | window requests used: {}".format(
                    page + 1, total_pages, cp["requests_in_window"]))

                # Checkpoint every 10 pages so an interruption loses little work
                if page % 10 == 9:
                    save_year_data(year, year_data)
                    cp["current_year"] = year
                    cp["batch_idx"]    = b_idx
                    cp["page"]         = page
                    save_checkpoint(cp)

                page += 1
                if page >= total_pages:
                    break   # batch done

            if rate_limited:
                all_done = False
                break

        if rate_limited:
            break

        # ── Year complete ────────────────────────────────────────────────────
        write_year_csv(year, year_data)
        completed.add(year)
        cp["completed_years"] = list(completed)
        cp["current_year"]    = year
        cp["batch_idx"]       = 0
        cp["page"]            = 0
        save_checkpoint(cp)

        # Clean up the temp data file for this year
        _unlink(TEMP_DIR / "{}_data.json".format(year))

    if all_done:
        log("All {} years collected successfully!".format(len(API_YEARS)))
        log("CSVs are in: {}".format(OUTPUT_DIR))

        # Clean up state files
        _unlink(CHECKPOINT)
        for f in TEMP_DIR.glob("*.json"):
            _unlink(f)

        remove_scheduler()


if __name__ == "__main__":
    main()
