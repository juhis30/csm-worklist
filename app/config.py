import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("WORKLIST_DB", ROOT / "data" / "worklist.db"))
