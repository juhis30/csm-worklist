import json
import re

import pytest
from fastapi.testclient import TestClient

from app import web


@pytest.fixture
def client(world_db):
    web._conn = world_db
    return TestClient(web.app)


def login(client, user_id):
    assert client.post("/login", data={"user_id": user_id}, follow_redirects=False).status_code == 303
    return client


@pytest.mark.parametrize("user_id", ["northwind-csm01", "brightpath-lead", "cobalt-csm02"])
def test_every_page_renders_for_each_persona(world_db, client, user_id):
    login(client, user_id)
    for path in ["/", "/accounts", "/accounts?mine=0&q=a", "/runs", "/model", "/data"]:
        assert client.get(path).status_code == 200, path
    tenant = user_id.split("-")[0]
    s = world_db.execute("SELECT account_id, score_id FROM scores WHERE tenant_id = ? ORDER BY score_id DESC LIMIT 1",
                         (tenant,)).fetchone()
    assert client.get(f"/accounts/{s['account_id']}").status_code == 200
    assert client.get(f"/accounts/{s['account_id']}/evidence?feature=tickets_90d&score_id={s['score_id']}").status_code == 200


def test_signed_out_users_are_sent_to_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_another_orgs_account_score_and_task_are_invisible(world_db, client):
    other = world_db.execute("SELECT account_id, score_id FROM scores WHERE tenant_id = 'brightpath' LIMIT 1").fetchone()
    task = world_db.execute("SELECT task_id, status FROM tasks WHERE tenant_id = 'brightpath' AND status = 'open' LIMIT 1").fetchone()
    login(client, "northwind-lead")
    assert client.get(f"/accounts/{other['account_id']}").status_code == 404
    assert client.get(f"/accounts/{other['account_id']}/evidence?feature=usage_30d&score_id={other['score_id']}").status_code == 404
    assert client.post(f"/tasks/{task['task_id']}/dismiss").status_code == 404
    assert world_db.execute("SELECT status FROM tasks WHERE task_id = ?", (task["task_id"],)).fetchone()[0] == "open"
    page = client.get(f"/accounts?mine=0&q={other['account_id']}").text
    assert f'href="/accounts/{other["account_id"]}"' not in page


def test_only_the_owning_csm_can_send_and_a_double_send_sends_once(world_db, client):
    t = world_db.execute("SELECT * FROM tasks WHERE tenant_id = 'northwind' AND status = 'open' "
                         "AND draft_to IS NOT NULL AND csm_id IS NOT NULL LIMIT 1").fetchone()
    other_csm = world_db.execute("SELECT user_id FROM users WHERE tenant_id = 'northwind' AND role = 'csm' "
                                 "AND user_id != ? LIMIT 1", (t["csm_id"],)).fetchone()[0]
    assert login(client, other_csm).post(f"/tasks/{t['task_id']}/send").status_code == 403
    login(client, t["csm_id"])
    for _ in range(2):
        client.post(f"/tasks/{t['task_id']}/send", data={"subject": "Hi", "body": "Hello from {csm}"})
    sent = world_db.execute("SELECT * FROM outbox WHERE task_id = ?", (t["task_id"],)).fetchall()
    assert len(sent) == 1 and sent[0]["sent_by"] == t["csm_id"] and "{csm}" not in sent[0]["body"]


def test_evidence_lists_exactly_the_events_behind_the_value(world_db, client):
    s = world_db.execute(
        "SELECT * FROM scores WHERE tenant_id = 'northwind' AND json_extract(features, '$.usage_30d') BETWEEN 2 AND 250 "
        "AND json_extract(features, '$.tickets_90d') >= 1 ORDER BY score_id DESC LIMIT 1").fetchone()
    login(client, "northwind-lead")
    for feature in ("usage_30d", "usage_prior_60d", "tickets_90d"):
        page = client.get(f"/accounts/{s['account_id']}/evidence?feature={feature}&score_id={s['score_id']}").text
        shown = int(re.search(r"Events the score used \((\d+)", page).group(1))
        assert shown == json.loads(s["features"])[feature], feature


def test_scheduler_endpoint_needs_the_token(client):
    assert client.post("/internal/agent/run").status_code == 401
    assert client.post("/internal/agent/run", headers={"Authorization": "Bearer wrong"}).status_code == 401
