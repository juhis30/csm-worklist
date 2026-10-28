import json

import pytest

from app import db, ingest

T = "2026-01-10T00:00:00Z"


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    c.execute("INSERT INTO tenants VALUES ('t1', 'T1', '{}')")
    c.execute("INSERT INTO accounts (tenant_id, account_id, name, domain, contract_start, workspace_id, contact_email) "
              "VALUES ('t1', 'A1', 'Acme', 'https://www.Acme.com/', '2025-01-01', 'ws_1', 'jo.acme@gmail.com')")
    return c


def land(conn, source, sid, payload, ingested_at=T):
    conn.execute("INSERT INTO raw_events (tenant_id, source, source_event_id, payload, ingested_at) "
                 "VALUES ('t1', ?, ?, ?, ?)", (source, sid, json.dumps(payload), ingested_at))


def stripe(email):
    return {"type": "invoice.paid", "created": 1767225600, "data": {"object": {"customer_email": email, "amount": 100}}}


@pytest.mark.parametrize("raw,expected", [
    ("https://www.Acme.com/", "acme.com"), ("WWW.ACME.COM", "acme.com"), ("jo@acme.com", "acme.com"),
    ("acme.com/pricing", "acme.com"), ("", None), (None, None),
])
def test_normalize_domain(raw, expected):
    assert ingest.normalize_domain(raw) == expected


def test_every_source_format_resolves_to_the_same_account_in_utc(conn):
    land(conn, "product", "p1", {"workspace_id": "ws_1", "event": "login", "user_id": "u1", "ts": 1767225600000})
    land(conn, "zendesk", "z1", {"type": "ticket.created", "ticket": {"id": 7, "priority": "urgent",
                                 "organization": "WWW.ACME.COM"}, "updated_at": "2026-01-01T00:00:00Z"})
    land(conn, "stripe", "s1", {"type": "invoice.payment_failed", "created": 1767225600,
                                "data": {"object": {"customer_email": "billing@acme.com", "amount": 12345}}})
    land(conn, "gmail", "g1", {"direction": "in", "from": "jo@acme.com", "to": "csm@t1.example",
                               "date": "Thu, 01 Jan 2026 05:30:00 +0530"})
    land(conn, "salesforce", "o1", {"Id": "o1", "AccountId": "A1", "Type": "Renewal", "StageName": "Closed Lost",
                                    "CloseDate": "2026-01-01", "Renewal_Date__c": "2026-01-05", "Amount": 5000})
    stats = ingest.ingest(conn, "t1", "2026-02-01T00:00:00Z")
    assert sum(v for k, v in stats.items() if k.endswith("accepted")) == 5
    rows = {r["source"]: r for r in conn.execute("SELECT * FROM events")}
    assert {r["account_id"] for r in rows.values()} == {"A1"}
    assert {r["occurred_at"] for r in rows.values()} == {"2026-01-01T00:00:00Z"}
    assert rows["zendesk"]["value"] == 4 and rows["stripe"]["value"] == 123.45
    assert rows["salesforce"]["event_type"] == "renewal_lost" and rows["salesforce"]["ref"] == "2026-01-05"


def test_first_delivery_wins_and_rerun_is_noop(conn):
    payload = {"workspace_id": "ws_1", "ts": 1767225600000}
    land(conn, "product", "p1", payload, "2026-01-10T00:00:00Z")
    land(conn, "product", "p1", payload, "2026-01-12T00:00:00Z")
    assert ingest.ingest(conn, "t1", "2026-02-01T00:00:00Z") == {"product.accepted": 1, "product.duplicate": 1}
    assert conn.execute("SELECT ingested_at FROM events").fetchone()[0] == "2026-01-10T00:00:00Z"
    assert ingest.ingest(conn, "t1", "2026-02-01T00:00:00Z") == {}


def test_events_not_yet_arrived_wait_for_a_later_run(conn):
    land(conn, "product", "p1", {"workspace_id": "ws_1", "ts": 1767225600000}, "2026-03-01T00:00:00Z")
    assert ingest.ingest(conn, "t1", "2026-02-01T00:00:00Z") == {}
    assert ingest.ingest(conn, "t1", "2026-03-02T00:00:00Z") == {"product.accepted": 1}


def test_personal_email_matches_only_a_known_contact(conn):
    land(conn, "stripe", "s1", stripe("Jo.Acme@gmail.com"))        # known contact on a personal address
    land(conn, "stripe", "s2", stripe("stranger@gmail.com"))
    ingest.ingest(conn, "t1", "2026-02-01T00:00:00Z")
    assert conn.execute("SELECT account_id FROM events").fetchall()[0][0] == "A1"
    assert conn.execute("SELECT reason FROM rejected_events").fetchone()[0] == "personal_email"


def test_every_rejection_has_a_reason(conn):
    land(conn, "product", "p1", {"workspace_id": "ws_unknown", "ts": 1767225600000})
    land(conn, "product", "p2", {"workspace_id": "ws_1", "ts": None})
    land(conn, "zendesk", "z1", {"type": "ticket.created", "ticket": {"id": 1, "organization": "prospect.com"},
                                 "updated_at": "2026-01-01T00:00:00Z"})
    land(conn, "gmail", "g1", "not even an object")
    land(conn, "stripe", "s1", {**stripe("x@acme.com"), "created": 1893456000})   # 2030, after arrival
    ingest.ingest(conn, "t1", "2026-02-01T00:00:00Z")
    reasons = sorted(r[0] for r in conn.execute("SELECT reason FROM rejected_events"))
    assert reasons == ["bad_timestamp", "future_timestamp", "malformed", "unmapped_workspace", "unresolved_account"]
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_crm_fix_recovers_rejected_events_as_of_the_fix(conn):
    land(conn, "zendesk", "z1", {"type": "ticket.created", "ticket": {"id": 1, "organization": "acme-labs.io"},
                                 "updated_at": "2026-01-01T00:00:00Z"})
    ingest.ingest(conn, "t1", "2026-02-01T00:00:00Z")
    assert conn.execute("SELECT reason FROM rejected_events").fetchone()[0] == "unresolved_account"

    # Someone adds the missing account to Salesforce.
    conn.execute("INSERT INTO accounts (tenant_id, account_id, name, domain, contract_start) "
                 "VALUES ('t1', 'A2', 'Acme Labs', 'acme-labs.io', '2025-01-01')")
    assert ingest.ingest(conn, "t1", "2026-02-08T00:00:00Z") == {"zendesk.recovered": 1}
    e = conn.execute("SELECT account_id, ingested_at FROM events").fetchone()
    assert e["account_id"] == "A2" and e["ingested_at"] == "2026-02-08T00:00:00Z"   # usable only from the fix
    assert conn.execute("SELECT COUNT(*) FROM rejected_events").fetchone()[0] == 0
