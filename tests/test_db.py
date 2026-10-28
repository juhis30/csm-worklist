import sqlite3

import pytest

from app import db


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    for t in ("acme", "globex"):
        c.execute("INSERT INTO tenants VALUES (?, ?, '{}')", (t, t.title()))
        c.execute("INSERT INTO accounts (tenant_id, account_id, name, contract_start) VALUES (?, 'A1', ?, '2025-01-01')",
                  (t, f"{t} customer"))
    return c


def test_scope_only_returns_own_tenant_rows(conn):
    rows = db.TenantScope(conn, "acme").all("SELECT name FROM accounts WHERE tenant_id = :tenant_id")
    assert [r["name"] for r in rows] == ["acme customer"]


def test_scope_refuses_query_without_tenant_filter(conn):
    with pytest.raises(ValueError):
        db.TenantScope(conn, "acme").all("SELECT * FROM accounts")


def test_scope_refuses_caller_supplied_tenant(conn):
    with pytest.raises(ValueError):
        db.TenantScope(conn, "acme").all("SELECT * FROM accounts WHERE tenant_id = :tenant_id", tenant_id="globex")


def test_transaction_rolls_back_on_error(conn):
    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            conn.execute("INSERT INTO tenants VALUES ('initech', 'Initech', '{}')")
            raise RuntimeError("crash mid-run")
    assert conn.execute("SELECT COUNT(*) FROM tenants").fetchone()[0] == 2


def test_redelivered_event_cannot_be_inserted_twice(conn):
    conn.execute("INSERT INTO raw_events (tenant_id, source, payload, ingested_at) VALUES ('acme', 'stripe', '{}', 'x')")
    row = ("acme", "stripe", "evt_1", "A1", "invoice_paid", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", 1)
    sql = ("INSERT INTO events (tenant_id, source, source_event_id, account_id, event_type, occurred_at, ingested_at, raw_id) "
           "VALUES (?, ?, ?, ?, ?, ?, ?, ?)")
    conn.execute(sql, row)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, row)
