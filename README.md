# College Scorecard Historical Data Pull

Downloads all available fields for every academic year (1996–97 through 2025–26) from the [College Scorecard API](https://collegescorecard.ed.gov/data/api-documentation/). Output is one CSV per year in an `output/` folder.

**Estimated run time:** ~92 hours of active pulling (~5 calendar days). The script is fully automated after the first run.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.6+ | Must be on your PATH (or available in your Jupyter environment) |
| `requests` library | `pip install requests` |
| API key | Free — register at [api.data.gov/signup](https://api.data.gov/signup) |

---

## Setup (all platforms)

1. **Install the dependency**

   ```
   pip install requests
   ```

2. **Add your API key** — open `scorecard_pull.py` and replace `YOUR_API_KEY_HERE` on line 35:

   ```python
   API_KEY = "abc123yourkey"
   ```

3. **Verify the `API_Documentation/` folder** is in the same directory as the script (it should already be there).

---

## Running on Mac

Just run the script once. It automatically installs itself into `crontab` so it restarts every 15 minutes without any further action from you.

```bash
python3 scorecard_pull.py
```

**Leave your laptop plugged in and powered on.** The script manages all rate-limit pauses on its own.

To monitor progress:

```bash
tail -f scorecard_pull.log
```

To stop early and remove the schedule:

```bash
# Remove the automatic schedule
crontab -l | grep -v scorecard_pull | crontab -

# The checkpoint.json and temp/ folder preserve your progress.
# Re-run the script later to resume from where it stopped.
```

---

## Running on Windows (Task Scheduler)

The script will attempt to create a Task Scheduler task automatically on first run. If that fails due to permissions, follow the manual steps below.

### Option A — Automatic (try this first)

Open **Command Prompt** and run:

```
python scorecard_pull.py
```

If the script prints `Task Scheduler task 'CollegeScorecardPull' created`, you're done. Leave the PC on and it will handle everything.

---

### Option B — Manual Task Scheduler setup

Use this if Option A fails or if you prefer to configure it yourself.

#### Step 1 — Find your Python path

In Command Prompt:

```
where python
```

Note the full path, e.g. `C:\Users\YourName\AppData\Local\Programs\Python\Python311\python.exe`

#### Step 2 — Open Task Scheduler

Press `Win + S`, search for **Task Scheduler**, and open it.

#### Step 3 — Create a new task

1. In the right panel, click **Create Task** (not "Create Basic Task").

2. **General tab**
   - Name: `CollegeScorecardPull`
   - Select **Run whether user is logged on or not**
   - Check **Run with highest privileges** (only if you have admin rights; otherwise leave unchecked)

3. **Triggers tab** — click **New**
   - Begin the task: **On a schedule**
   - Settings: **Daily**, start today at a convenient time
   - Check **Repeat task every:** `15 minutes` for a duration of **Indefinitely**
   - Click OK

4. **Actions tab** — click **New**
   - Action: **Start a program**
   - Program/script: full path to your Python executable (from Step 1)
     ```
     C:\Users\YourName\AppData\Local\Programs\Python\Python311\python.exe
     ```
   - Add arguments:
     ```
     "C:\full\path\to\scorecard_pull.py"
     ```
   - Start in (optional but recommended): the folder containing the script
     ```
     C:\full\path\to\college_scorecard_historical_data_pull
     ```
   - Click OK

5. **Conditions tab**
   - Uncheck **Start the task only if the computer is on AC power** (so it runs on battery too, if needed)

6. Click **OK** to save the task. Enter your Windows password if prompted.

#### Step 4 — Run the script once to start

In Command Prompt (navigate to the script folder first):

```
cd C:\full\path\to\college_scorecard_historical_data_pull
python scorecard_pull.py
```

The first run starts the data pull. From then on, Task Scheduler fires every 15 minutes automatically.

---

### Monitoring progress on Windows

```
type scorecard_pull.log
```

Or open `scorecard_pull.log` in Notepad/Excel at any time — it appends a line for every page fetched.

### Stopping early on Windows

In Task Scheduler, right-click `CollegeScorecardPull` → **Delete**.

Your progress is saved in `checkpoint.json` and `temp/`. Delete those files only if you want to start over from scratch.

---

## Running from Jupyter Notebook

Both scripts are Jupyter-compatible and work on Mac and Windows. Open your notebook from the folder that contains `scorecard_pull.py` and `API_Documentation/`.

### Start the data pull

```python
import scorecard_pull as sc
sc.main()
```

`main()` returns cleanly when it hits the rate limit or finishes — it does not kill the kernel. The first call installs the Task Scheduler task (Windows) or crontab entry (Mac) so that subsequent restarts are handled automatically, just as they would be from the command line.

### Run the test suite

```python
import test_scorecard_pull
result = test_scorecard_pull.run_tests()
```

Or use `%run` to execute either file directly in a cell:

```
%run scorecard_pull.py
%run test_scorecard_pull.py
```

### Run live API connectivity tests

Once your API key is configured in `scorecard_pull.py`, the live tests run automatically alongside the unit tests. You can also supply the key via environment variable without editing the file:

```python
import os
os.environ["SCORECARD_API_KEY"] = "your_key_here"

import test_scorecard_pull
test_scorecard_pull.run_tests()
```

The live tests make ~10 real API requests and verify connectivity, field names, and pagination.

---

## Output

| File | Contents |
|---|---|
| `output/scorecard_1996_1997.csv` | All institutions, all fields for 1996–97 |
| `output/scorecard_1997_1998.csv` | All institutions, all fields for 1997–98 |
| … | … |
| `output/scorecard_2025_2026.csv` | All institutions, all fields for 2025–26 |

Each CSV has `id` (UNITID) as the first column, followed by static school fields (`school.name`, `school.city`, etc.) and then all year-specific fields prefixed with the year (e.g., `1996.admissions.admission_rate.overall`).

---

## How the rate limiting works

The College Scorecard API allows 1,000 requests per hour. The script:

1. Tracks how many requests it has made in the current hour
2. When it reaches 950 (a safe buffer), saves its exact position and exits
3. Task Scheduler (or crontab) relaunches it every 15 minutes
4. On each relaunch, it checks whether the hold period has expired; if not, it exits immediately (no wasted requests)
5. Once the hour window resets, it resumes exactly where it left off

No requests are ever lost or duplicated.

---

## Running the tests

The test suite covers all key functionality: checkpoint save/load, rate-limit logic, CSV output, pagination, scheduler install/remove, and API connectivity.

```bash
# Mac / Linux
python3 test_scorecard_pull.py

# Windows Command Prompt
python test_scorecard_pull.py

# With pytest (optional, prettier output)
pip install pytest
pytest test_scorecard_pull.py -v
```

86 tests run by default. The 11 live API tests are skipped automatically until an API key is configured (see "Run live API connectivity tests" above).

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `API_KEY not configured` message in log | Edit line 35 of `scorecard_pull.py` with your key |
| Script runs but no progress in log | Check that the Task Scheduler task is enabled and the Python path is correct |
| `schtasks` error on first run | Use the manual Task Scheduler setup (Option B above) |
| Log shows repeated `hold active` lines | Normal — it's waiting for the rate-limit window to reset |
| Want to restart from scratch | Delete `checkpoint.json` and the `temp/` folder |
| Live API tests still skipped after setting key | Make sure you restarted the Python session / re-imported after setting the env var |
