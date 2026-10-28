import json
import math
from datetime import date, timedelta

from app import agent, db


def latest(conn, tenant):
    iso = conn.execute("SELECT MAX(as_of) FROM agent_runs WHERE tenant_id = ? AND status = 'succeeded'",
                       (tenant,)).fetchone()[0]
    return date.fromisoformat(iso[:10])


def counts(conn):
    return [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("scores", "tasks", "agent_runs")]


def test_bootstrap_runs_succeeded_for_every_org(world_db):
    rows = world_db.execute("SELECT tenant_id, COUNT(*) FROM agent_runs WHERE status = 'succeeded' GROUP BY tenant_id").fetchall()
    assert {r[0]: r[1] for r in rows} == {"northwind": 2, "brightpath": 2, "cobalt": 2}


def test_rerunning_a_finished_week_changes_nothing(world_db):
    before = counts(world_db)
    assert agent.run_tenant(world_db, "northwind", latest(world_db, "northwind"), "manual")["status"].startswith("skipped")
    assert counts(world_db) == before


def test_crash_leaves_no_partial_week_and_retry_succeeds(world_db, monkeypatch):
    as_of = latest(world_db, "brightpath") + timedelta(days=7)
    pending = "SELECT COUNT(*) FROM raw_events WHERE tenant_id = 'brightpath' AND processed = 0"
    pending_before = world_db.execute(pending).fetchone()[0]

    def boom(*a, **k):
        raise RuntimeError("feature store unavailable")

    monkeypatch.setattr(agent, "build_features", boom)
    failed = agent.run_tenant(world_db, "brightpath", as_of, "schedule")
    assert failed["status"] == "failed"
    log = world_db.execute("SELECT status, log FROM agent_runs WHERE run_id = ?", (failed["run_id"],)).fetchone()
    assert log["status"] == "failed" and "feature store unavailable" in log["log"]
    assert world_db.execute("SELECT COUNT(*) FROM scores WHERE run_id = ?", (failed["run_id"],)).fetchone()[0] == 0
    assert world_db.execute(pending).fetchone()[0] == pending_before          # ingestion rolled back too

    monkeypatch.undo()
    assert agent.run_tenant(world_db, "brightpath", as_of, "schedule")["status"] == "succeeded"
    assert world_db.execute("SELECT COUNT(*) FROM agent_runs WHERE tenant_id = 'brightpath' AND as_of = ?",
                            (agent.as_of_iso(as_of),)).fetchone()[0] == 1


def test_stored_scores_recompute_from_model_and_contributions(world_db):
    params = {r[0]: json.loads(r[1]) for r in world_db.execute("SELECT model_id, params FROM model_versions")}
    for model_id, p, contrib in world_db.execute("SELECT model_id, p_lost, contributions FROM scores LIMIT 500"):
        logit = params[model_id]["intercept"] + sum(json.loads(contrib).values())
        assert math.isclose(1 / (1 + math.exp(-logit)), p, abs_tol=1e-9)


def test_second_week_compares_with_the_first(world_db):
    logs = [json.loads(r[0]) for r in world_db.execute(
        "SELECT log FROM agent_runs WHERE tenant_id = 'northwind' AND status = 'succeeded' ORDER BY as_of")]
    assert next(s for s in logs[0] if s["step"] == "tasks")["compared_with_run"] is None
    assert next(s for s in logs[1] if s["step"] == "tasks")["compared_with_run"] is not None


def test_no_account_ever_has_two_open_tasks(world_db):
    assert not world_db.execute("SELECT account_id FROM tasks WHERE status = 'open' "
                                "GROUP BY tenant_id, account_id HAVING COUNT(*) > 1").fetchall()


def test_blind_spot_tasks_never_draft_a_customer_email(world_db):
    assert world_db.execute("SELECT COUNT(*) FROM tasks WHERE kind = 'blind_spot' AND draft_to IS NOT NULL").fetchone()[0] == 0


def test_stale_source_is_detected():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    conn.execute("INSERT INTO raw_events (tenant_id, source, payload, ingested_at) VALUES ('t', 'product', '{}', 'x')")
    conn.execute("INSERT INTO events VALUES ('t', 'product', 'e1', 'A', 'usage', '2026-01-01T00:00:00Z', "
                 "'2026-01-01T00:00:00Z', NULL, NULL, NULL, 1)")
    stale = agent.check_data_health(conn, "t", "2026-01-10T06:00:00Z",
                                    {"product": True, "zendesk": True, "gmail": False, "salesforce": True})
    assert stale == {"product": 9, "zendesk": None}
