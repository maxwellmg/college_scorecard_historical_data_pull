# College Scorecard Historical Data Pull

Downloads all available fields for every academic year (1996–97 through 2025–26) from the [College Scorecard API](https://collegescorecard.ed.gov/data/api-documentation/). Output is one CSV per year in an `output/` folder.

**Estimated run time:** ~92 hours of active pulling (~5 calendar days). The script is fully automated after the first run.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.6+ | Must be on your PATH |
| `requests` library | `pip install requests` |
| API key | Free — register at [api.data.gov/signup](https://api.data.gov/signup) |

---

## Setup (both platforms)

1. **Install the dependency**

   ```
   pip install requests
   ```

2. **Add your API key** — open `scorecard_pull.py` and replace `YOUR_API_KEY_HERE` on line 48:

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
     C:\full\path\to\College_Scorecard_Data_Pull
     ```
   - Click OK

5. **Conditions tab**
   - Uncheck **Start the task only if the computer is on AC power** (so it runs on battery too, if needed)

6. Click **OK** to save the task. Enter your Windows password if prompted.

#### Step 4 — Run the script once to start

In Command Prompt (navigate to the script folder first):

```
cd C:\full\path\to\College_Scorecard_Data_Pull
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

## Troubleshooting

| Symptom | Fix |
|---|---|
| `API_KEY not configured` error | Edit line 48 of `scorecard_pull.py` with your key |
| Script runs but no progress in log | Check that Task Scheduler task is enabled and the Python path is correct |
| `schtasks` error on first run | Use the manual Task Scheduler setup (Option B above) |
| Log shows repeated `hold active` lines | Normal — it's waiting for the rate-limit window to reset |
| Want to restart from scratch | Delete `checkpoint.json` and the `temp/` folder |
