"""The unattended weekly run.

For each organisation: ingest -> data health -> retrain if due -> score every
active account -> compare with last week -> open CSM tasks with drafted
outreach. Nothing goes to a customer without a CSM approving it.

Guarantees:
  * Safe to re-run: one run per (tenant, as_of), enforced by the database.
    Re-running a succeeded week is a no-op; a failed week is cleared and redone.
  * All or nothing: the work is one transaction, so a crash never leaves half
    a week of scores. The run row is committed first, so failures stay visible.
  * Every step appends a record to the run log, which the UI shows.
"""

import json
import sys
import time
import traceback
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from app import config, db, ingest, outreach
from app.features import (FEATURE_BY_NAME, MISSING_GROUPS, build_features, corroboration_required,
                          load_accounts, load_events)
from app.model import INPUTS, active_model, save_model, score, train_and_evaluate

RETRAIN_AFTER_DAYS = 28
MIN_NEW_OUTCOMES = 20
AUC_TOLERANCE = 0.02       # a retrained model may be this much worse and still replace the old one
TOP_OF_BOOK = 10           # "entered top 10 by expected ARR at risk"
RISK_JUMP = 0.10           # "P(lost) rose by 10+ points"
BLIND_SPOT_HORIZON = 90    # days to renewal
TASK_COOLDOWN_DAYS = 21
TASK_EXPIRY_DAYS = 28
STALE_AFTER_DAYS = 4


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def as_of_iso(d: date) -> str:
    return f"{d.isoformat()}T06:00:00Z"   # Monday 06:00 UTC, before CSMs start


def next_as_of(conn) -> date:
    if config.CLOCK_MODE == "wall":
        return datetime.now(timezone.utc).date()
    current = db.get_meta(conn, "clock")
    if current is None:
        return config.DATA_END - timedelta(weeks=config.BOOTSTRAP_WEEKS_BEHIND)
    return min(date.fromisoformat(current) + timedelta(days=7), config.DATA_END)


class RunLog:
    def __init__(self):
        self.steps = []

    def step(self, name, **detail):
        self.steps.append({"step": name, "at": now_iso(), **detail})

    def dump(self):
        return json.dumps(self.steps, default=str)


# ------------------------------------------------------------------ steps

def check_data_health(conn, tenant_id, as_of, integrations):
    """Connected sources that have gone quiet. A broken feed looks exactly
    like customers going quiet, so we must not raise alarms off it."""
    stale = {}
    for source, enabled in integrations.items():
        if not enabled or source == "salesforce":
            continue
        last = conn.execute("SELECT MAX(ingested_at) FROM events WHERE tenant_id = ? AND source = ?",
                            (tenant_id, source)).fetchone()[0]
        age = None if last is None else (pd.Timestamp(as_of) - pd.Timestamp(last)).days
        if age is None or age > STALE_AFTER_DAYS:
            stale[source] = age
    return stale


def maybe_retrain(conn, tenant_id, as_of, events, accounts, gmail_enabled, log):
    """Retrain every 4 weeks once 20+ new outcomes exist; keep the old model if the new one is clearly worse."""
    row, _ = active_model(conn, tenant_id)
    if row is not None:
        age = (pd.Timestamp(as_of) - pd.Timestamp(row["trained_at"])).days
        new_outcomes = conn.execute(
            "SELECT COUNT(*) FROM events WHERE tenant_id = ? AND event_type IN ('renewal_won', 'renewal_lost') "
            "AND ingested_at > ? AND ingested_at <= ?", (tenant_id, row["trained_at"], as_of)).fetchone()[0]
        if age < RETRAIN_AFTER_DAYS or new_outcomes < MIN_NEW_OUTCOMES:
            log.step("model", action="kept", model_id=row["model_id"], age_days=age, new_outcomes=new_outcomes)
            return row["model_id"]

    t0 = time.time()
    model, metrics = train_and_evaluate(events, accounts, as_of, gmail_enabled)
    old_id = row["model_id"] if row else None
    if model is None:
        log.step("model", action="training_failed", reason=metrics["error"], kept=old_id)
        return old_id

    new_auc = metrics["model"]["auc"]
    if row is not None:
        old_auc = json.loads(row["metrics"])["model"]["auc"]
        if new_auc < old_auc - AUC_TOLERANCE:
            rejected = save_model(conn, tenant_id, model, metrics, as_of, "rejected",
                                  f"test AUC {new_auc:.3f} vs active {old_auc:.3f}")
            log.step("model", action="new_model_rejected", rejected=rejected, kept=old_id,
                     new_auc=round(new_auc, 4), active_auc=round(old_auc, 4))
            return old_id
        conn.execute("UPDATE model_versions SET status = 'retired' WHERE tenant_id = ? AND status = 'active'",
                     (tenant_id,))
    new_id = save_model(conn, tenant_id, model, metrics, as_of)
    log.step("model", action="trained", model_id=new_id, replaced=old_id, auc=round(new_auc, 4),
             validated=metrics["validation"]["validated"], seconds=round(time.time() - t0, 1))
    return new_id


def write_scores(conn, tenant_id, run_id, as_of, model_id, scored):
    keep = [n for n in INPUTS] + ["active_users_30d"]          # inputs, plus context shown to CSMs
    rows = []
    for account_id, r in scored.iterrows():
        feats = {c: (None if pd.isna(r[c]) else float(r[c])) for c in keep}
        coverage = {s: int(r[f"n_{s}_180d"]) for s in ("product", "zendesk", "stripe", "gmail")}
        rows.append((tenant_id, run_id, account_id, as_of, model_id, float(r["p_lost"]),
                     None if pd.isna(r["arr"]) else float(r["arr"]), float(r["arr_at_risk"]), r["renewal_date"],
                     int(r["blind_spot"]), json.dumps(feats), r["contributions"], json.dumps(coverage)))
    conn.executemany(
        "INSERT INTO scores (tenant_id, run_id, account_id, as_of, model_id, p_lost, arr, arr_at_risk, renewal_date, "
        "blind_spot, features, contributions, coverage) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)


def strongest_driver(contributions: dict, features: dict):
    """(input name, source) of the input pushing risk up the most, skipping inputs that
    were only filled in because data was missing: their effect is the "no data" input's.
    A "no data" input only counts when the data really is missing."""
    real = {k: v for k, v in contributions.items()
            if (features.get(k) if k in MISSING_GROUPS else features.get(k) is not None)}
    name, value = max(real.items(), key=lambda kv: kv[1])
    if value <= 0:
        return None, None
    if name in MISSING_GROUPS:
        return name, "missing"
    return name, FEATURE_BY_NAME[name].source


def top_of_book(rows):
    """account_id set of each CSM's top 10 by expected ARR at risk (blind spots aren't ranked)."""
    by_csm = {}
    for r in sorted((r for r in rows if not r["blind_spot"]), key=lambda r: -r["arr_at_risk"]):
        by_csm.setdefault(r["csm_id"], [])
        if len(by_csm[r["csm_id"]]) < TOP_OF_BOOK:
            by_csm[r["csm_id"]].append(r["account_id"])
    return {a for top in by_csm.values() for a in top}


def expire_tasks(conn, tenant_id, as_of):
    """Close open tasks that are moot: the renewal they were about has passed,
    or nobody acted for 4 weeks. Frees the account for a fresh task if it still qualifies."""
    stale_before = (pd.Timestamp(as_of) - pd.Timedelta(days=TASK_EXPIRY_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return conn.execute(
        "UPDATE tasks SET status = 'dismissed', resolved_at = :as_of, resolved_by = 'agent:expired' "
        "WHERE tenant_id = :t AND status = 'open' AND (created_at < :stale_before "
        " OR (SELECT renewal_date FROM scores s WHERE s.score_id = tasks.score_id) < substr(:as_of, 1, 10))",
        {"t": tenant_id, "as_of": as_of, "stale_before": stale_before}).rowcount


def create_tasks(conn, tenant_id, run_id, as_of, stale_sources, log):
    expired = expire_tasks(conn, tenant_id, as_of)

    def scores_of(rid):
        return conn.execute(
            "SELECT s.*, a.name, a.csm_id, a.contact_name, a.contact_email FROM scores s JOIN accounts a "
            "ON a.tenant_id = s.tenant_id AND a.account_id = s.account_id WHERE s.tenant_id = ? AND s.run_id = ?",
            (tenant_id, rid)).fetchall()

    prev_run = conn.execute("SELECT run_id FROM agent_runs WHERE tenant_id = ? AND status = 'succeeded' "
                            "AND as_of < ? ORDER BY as_of DESC LIMIT 1", (tenant_id, as_of)).fetchone()
    current = scores_of(run_id)
    previous = {r["account_id"]: r for r in scores_of(prev_run[0])} if prev_run else {}
    top_now = top_of_book(current)
    top_before = top_of_book(previous.values())

    cooldown_from = (pd.Timestamp(as_of) - pd.Timedelta(days=TASK_COOLDOWN_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cooling = {r[0] for r in conn.execute(
        "SELECT account_id FROM tasks WHERE tenant_id = ? AND (status = 'open' OR created_at >= ?)",
        (tenant_id, cooldown_from))}

    created, skipped = {}, {"cooldown": 0, "stale_source": 0}
    for s in current:
        before = previous.get(s["account_id"])
        days_to_renewal = (pd.Timestamp(s["renewal_date"], tz="UTC") - pd.Timestamp(as_of)).days
        if s["blind_spot"]:
            kind = "blind_spot" if days_to_renewal <= BLIND_SPOT_HORIZON else None
        elif s["account_id"] in top_now and s["account_id"] not in top_before:
            kind = "entered_top10"
        elif before is not None and s["p_lost"] - before["p_lost"] >= RISK_JUMP:
            kind = "risk_jumped"
        else:
            kind = None
        if kind is None:
            continue
        if s["account_id"] in cooling:
            skipped["cooldown"] += 1
            continue
        features = json.loads(s["features"])
        driver, source = strongest_driver(json.loads(s["contributions"]), features)
        if source in stale_sources and kind != "blind_spot":
            skipped["stale_source"] += 1
            continue
        d = outreach.draft(kind, driver, s, features, days_to_renewal)
        conn.execute(
            "INSERT INTO tasks (tenant_id, account_id, csm_id, run_id, score_id, kind, reason, draft_to, "
            "draft_subject, draft_body, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tenant_id, s["account_id"], s["csm_id"], run_id, s["score_id"], kind, d["reason"],
             d["to"], d["subject"], d["body"], as_of))
        created[kind] = created.get(kind, 0) + 1
    log.step("tasks", created=created, expired=expired, skipped=skipped,
             compared_with_run=prev_run[0] if prev_run else None)


# ------------------------------------------------------------------ orchestration

def run_tenant(conn, tenant_id, as_of_date: date, trigger: str):
    as_of = as_of_iso(as_of_date)
    existing = conn.execute("SELECT run_id, status FROM agent_runs WHERE tenant_id = ? AND as_of = ?",
                            (tenant_id, as_of)).fetchone()
    if existing and existing["status"] == "succeeded":
        return {"tenant": tenant_id, "as_of": as_of, "status": "skipped (already done)", "run_id": existing["run_id"]}
    if existing:   # failed or crashed earlier: clear it and redo
        with db.transaction(conn):
            for table in ("tasks", "scores", "agent_runs"):
                conn.execute(f"DELETE FROM {table} WHERE run_id = ?", (existing["run_id"],))

    with db.transaction(conn):
        run_id = conn.execute("INSERT INTO agent_runs (tenant_id, as_of, trigger, status, started_at) "
                              "VALUES (?, ?, ?, 'running', ?)", (tenant_id, as_of, trigger, now_iso())).lastrowid
    log = RunLog()
    try:
        with db.transaction(conn):
            cfg = json.loads(conn.execute("SELECT config FROM tenants WHERE tenant_id = ?", (tenant_id,)).fetchone()[0])
            gmail = cfg["integrations"]["gmail"]
            log.step("ingest", **ingest.ingest(conn, tenant_id, as_of))
            stale = check_data_health(conn, tenant_id, as_of, cfg["integrations"])
            log.step("data_health", stale_sources=stale)

            events, accounts = load_events(conn, tenant_id), load_accounts(conn, tenant_id)
            model_id = maybe_retrain(conn, tenant_id, as_of, events, accounts, gmail, log)
            if model_id is None:
                raise RuntimeError("no usable model for this organisation; refusing to publish scores")
            _, model = active_model(conn, tenant_id)

            feats = build_features(events, accounts, pd.Timestamp(as_of), gmail, corroboration_required(cfg))
            scored = score(model, feats)
            write_scores(conn, tenant_id, run_id, as_of, model_id, scored)
            log.step("score", accounts=len(scored), blind_spots=int(scored["blind_spot"].sum()),
                     arr_at_risk=round(float(scored["arr_at_risk"].sum())), model_id=model_id)

            create_tasks(conn, tenant_id, run_id, as_of, stale, log)
            conn.execute("UPDATE agent_runs SET status = 'succeeded', finished_at = ?, log = ? WHERE run_id = ?",
                         (now_iso(), log.dump(), run_id))
        return {"tenant": tenant_id, "as_of": as_of, "status": "succeeded", "run_id": run_id}
    except Exception as e:
        log.step("error", error=repr(e), trace=traceback.format_exc()[-1500:])
        conn.execute("UPDATE agent_runs SET status = 'failed', finished_at = ?, log = ? WHERE run_id = ?",
                     (now_iso(), log.dump(), run_id))
        return {"tenant": tenant_id, "as_of": as_of, "status": "failed", "error": repr(e), "run_id": run_id}


def run_all(conn, trigger="schedule", as_of: date | None = None):
    """One scheduled tick: every organisation, same week. One failing doesn't stop the others."""
    as_of = as_of or next_as_of(conn)
    results = [run_tenant(conn, t[0], as_of, trigger)
               for t in conn.execute("SELECT tenant_id FROM tenants ORDER BY tenant_id").fetchall()]
    with db.transaction(conn):
        db.set_meta(conn, "clock", as_of.isoformat())
    return results


if __name__ == "__main__":
    for r in run_all(db.connect(), trigger=sys.argv[1] if len(sys.argv) > 1 else "manual"):
        print(r)
