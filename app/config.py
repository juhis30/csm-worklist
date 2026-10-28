import os
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("WORKLIST_DB", ROOT / "data" / "worklist.db"))

# The synthetic world: three years of history ending on this Monday.
DATA_END = date(2026, 10, 26)
HISTORY_YEARS = 3
SEED = 42

# Fraction of accounts to generate; tests use a smaller world.
SCALE = float(os.environ.get("WORKLIST_SCALE", "1.0"))

# "simulated": each scheduled run advances the clock one week, until DATA_END (demo).
# "wall": as_of is today (how it would run in production).
CLOCK_MODE = os.environ.get("WORKLIST_CLOCK", "simulated")
BOOTSTRAP_WEEKS_BEHIND = 3

# Shared secret between the scheduler (GitHub Actions) and the app.
AGENT_TOKEN = os.environ.get("WORKLIST_AGENT_TOKEN", "dev-token")
