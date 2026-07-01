import subprocess

# Step 1: remove the Task Scheduler task so no new runs start
r = subprocess.run(
    ['schtasks', '/delete', '/tn', 'CollegeScorecardPull', '/f'],
    capture_output=True, text=True
)
print("Task Scheduler:", r.stdout.strip() or r.stderr.strip())

# Step 2: kill any Python processes currently running scorecard_pull.py
r = subprocess.run(
    ['wmic', 'process', 'where',
     "name='python.exe' and commandline like '%scorecard_pull%'",
     'delete'],
    capture_output=True, text=True
)
print("Processes killed:", r.stdout.strip() or r.stderr.strip() or "none found")