import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app import config

SCHEMA = Path(__file__).with_name("schema.sql")


def connect(path=None) -> sqlite3.Connection:
    path = path or config.DB_PATH
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None: no hidden transactions. We open them explicitly
    # with transaction(), so every boundary is visible in the code.
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA.read_text())


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Commit if the block succeeds, roll back everything if it raises."""
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn, key, value):
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))


class TenantScope:
    """The only way the web app touches tenant data.

    Queries use named parameters and must reference :tenant_id. The scope
    binds it from the logged-in user's session, and a caller can't override
    it. A query that forgets the tenant filter fails loudly instead of
    leaking another organisation's rows.
    """

    def __init__(self, conn: sqlite3.Connection, tenant_id: str):
        self.conn = conn
        self.tenant_id = tenant_id

    def execute(self, sql: str, **params):
        if ":tenant_id" not in sql:
            raise ValueError("tenant-scoped query without :tenant_id")
        if "tenant_id" in params:
            raise ValueError("tenant_id is bound by the scope, not the caller")
        return self.conn.execute(sql, {**params, "tenant_id": self.tenant_id})

    def one(self, sql: str, **params):
        return self.execute(sql, **params).fetchone()

    def all(self, sql: str, **params):
        return self.execute(sql, **params).fetchall()
