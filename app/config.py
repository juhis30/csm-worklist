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
