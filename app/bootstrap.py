"""Build the demo database from scratch.

    python -m app.bootstrap               # world + first run 3 weeks back + catch up 2 weeks
    python -m app.bootstrap --catch-up 0

Leaves the last simulated week un-run, so the first scheduled run in the
deployed system has real new data to process.
"""

import argparse
import time
from pathlib import Path

from app import agent, config, datagen, db


def bootstrap(path=None, catch_up=2, scale=None, quiet=False):
    say = (lambda *a: None) if quiet else print
    path = Path(path or config.DB_PATH)
    for suffix in ("", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    conn = db.connect(path)
    db.init_schema(conn)

    t0 = time.time()
    world = datagen.World(scale=config.SCALE if scale is None else scale).build()
    with db.transaction(conn):
        datagen.load(conn, world)
    say(f"generated {len(world.accounts)} accounts, {len(world.raw)} raw events in {time.time() - t0:.0f}s")

    for i in range(catch_up + 1):
        t0 = time.time()
        for r in agent.run_all(conn, trigger="bootstrap"):
            say(" ", r)
        say(f"  week {i + 1} took {time.time() - t0:.0f}s")
    return conn


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--catch-up", type=int, default=2)
    bootstrap(catch_up=ap.parse_args().catch_up)
