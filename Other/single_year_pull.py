import sys
from pathlib import Path

# scorecard_pull.py lives one level up from this file
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scorecard_pull as sc

# Pulls 2023 only — blocks through any rate-limit pauses, returns when done
records = sc.main(year="2023")

# Optional: convert to DataFrame
import pandas as pd
df = pd.DataFrame(records)
df.head()
