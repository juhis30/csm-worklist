import json

import pytest

from app import datagen, db


@pytest.fixture(scope="module")
def world():
    return datagen.World(scale=0.05).build()


def test_same_seed_same_world():
    a = datagen.World(seed=7, scale=0.02).build()
    b = datagen.World(seed=7, scale=0.02).build()
    assert a.raw == b.raw and a.accounts == b.accounts


def test_all_five_sources_and_every_tenant_present(world):
    assert {r[1] for r in world.raw} == {"product", "zendesk", "stripe", "gmail", "salesforce"}
    assert {r[0] for r in world.raw} == {"northwind", "brightpath", "cobalt"}


def test_tenant_without_gmail_has_no_gmail_events(world):
    assert not [r for r in world.raw if r[0] == "brightpath" and r[1] == "gmail"]


def test_mess_is_present(world):
    s = datagen.summary(world)
    assert s["duplicates"] > 0
    assert s["crm_missing_domain"] + s["crm_missing_or_zero_arr"] + s["crm_missing_contact"] > 0
    payloads = [json.loads(r[3]) for r in world.raw]
    assert any(p.get("ts", 1) is None or p.get("updated_at", 1) is None or p.get("created", 1) is None
               for p in payloads), "some corrupted timestamps"
    # Each source points at accounts its own way.
    by_source = {r[1]: json.loads(r[3]) for r in world.raw}
    assert "workspace_id" in by_source["product"] and "AccountId" in by_source["salesforce"]
    assert "customer_email" in by_source["stripe"]["data"]["object"]


def test_loss_rate_is_plausible(world):
    assert 0.08 < datagen.summary(world)["renewal_loss_rate"] < 0.25


def test_loads_into_schema(world):
    conn = db.connect(":memory:")
    db.init_schema(conn)
    with db.transaction(conn):
        datagen.load(conn, world)
    assert conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0] == len(world.raw)
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == len(world.accounts)
