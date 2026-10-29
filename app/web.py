"""The CSM-facing app.

Tenant isolation: the organisation comes only from the signed session, never
from the URL or a form, and every read of tenant data goes through
db.TenantScope. Another organisation's account id returns 404, exactly like
an id that doesn't exist.
"""

import hmac
import json
import math
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app import agent, config, db
from app.features import FEATURE_BY_NAME, MISSING_GROUPS, MISSING_LABELS

app = FastAPI(title="CSM Worklist")
app.add_middleware(SessionMiddleware, secret_key=config.SESSION_SECRET, same_site="lax",
                   https_only=config.ON_RENDER)
templates = Jinja2Templates(directory=str(config.ROOT / "app" / "templates"))
_conn = None


def conn():
    global _conn
    if _conn is None:
        _conn = db.connect()
    return _conn


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ formatting

def money(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "-"
    return f"${v / 1000:,.0f}k" if abs(v) >= 1000 else f"${v:,.0f}"


def pct(v):
    return "-" if v is None else f"{v:.0%}"


def explain(score_row, params):
    """A stored score -> one row per model input: label, value, typical value, contribution.

    Typical = the training median, in the same units the CSM sees.
    """
    feats = json.loads(score_row["features"])
    rows = []
    for name, contrib in json.loads(score_row["contributions"]).items():
        if name in MISSING_GROUPS:
            if not feats.get(name):
                continue   # data present: the "no data" input is 0 and only shifts the baseline
            feature = FEATURE_BY_NAME[MISSING_GROUPS[name][0]]
            label, shown, typical = MISSING_LABELS[name], "yes", None
        else:
            feature = FEATURE_BY_NAME[name]
            value = feats.get(name)
            label = feature.label
            shown = "no data (typical value used)" if value is None else feature.fmt.format(value)
            median = params["medians"][name]
            typical = feature.fmt.format(math.expm1(median) if feature.log else median)
            if name == "log_arr":   # the model uses log ARR; people read dollars
                label, typical = "ARR", money(math.expm1(median))
                shown = shown if value is None else money(math.expm1(value))
        rows.append({"name": name, "feature": feature.name, "label": label, "value": shown, "typical": typical,
                     "contrib": contrib, "source": feature.source, "has_evidence": bool(feature.event_types),
                     "filled_in": name not in MISSING_GROUPS and feats.get(name) is None})
    return sorted(rows, key=lambda r: -abs(r["contrib"]))


def reasons(score_row, params, n=2, direction=1):
    """Top n inputs pushing risk up (direction=1) or down (-1), in plain language.

    Leaves out inputs that were only filled in because data was missing: their
    effect is already shown by the "no ... data" line, and listing both confuses.
    """
    rows = [r for r in explain(score_row, params) if r["contrib"] * direction > 0 and not r["filled_in"]]
    return sorted(rows, key=lambda r: -abs(r["contrib"]))[:n]


templates.env.filters.update(money=money, pct=pct, fromjson=json.loads)
templates.env.globals.update(explain=explain, reasons=reasons)


# ------------------------------------------------------------------ sign-in

class User:
    def __init__(self, row):
        self.id, self.tenant_id, self.name, self.role = row["user_id"], row["tenant_id"], row["name"], row["role"]
        self.scope = db.TenantScope(conn(), self.tenant_id)
        self.org_name = self.scope.one("SELECT name FROM tenants WHERE tenant_id = :tenant_id")["name"]


def current_user(request: Request) -> User:
    uid = request.session.get("user_id")
    row = conn().execute("SELECT * FROM users WHERE user_id = ?", (uid,)).fetchone() if uid else None
    if not row:
        raise HTTPException(303, headers={"Location": "/login"})
    return User(row)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    """Demo persona picker. A real deployment would use each customer's SSO."""
    users = conn().execute(
        "SELECT u.*, t.name AS org_name, (SELECT COUNT(*) FROM accounts a WHERE a.csm_id = u.user_id) AS n "
        "FROM users u JOIN tenants t USING (tenant_id) ORDER BY t.name, u.role DESC, u.user_id").fetchall()
    shown = [u for u in users if u["role"] == "leader" or u["user_id"][-2:] in ("01", "02", "03")]
    return templates.TemplateResponse(request, "login.html", {"users": shown})


@app.post("/login")
def login(request: Request, user_id: str = Form(...)):
    if not conn().execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)).fetchone():
        raise HTTPException(400, "unknown user")
    request.session["user_id"] = user_id
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ------------------------------------------------------------------ shared lookups

def week_context(u: User):
    """Latest successful run, the one before it, its model, and any warnings."""
    run = u.scope.one("SELECT * FROM agent_runs WHERE tenant_id = :tenant_id AND status = 'succeeded' "
                      "ORDER BY as_of DESC LIMIT 1")
    if not run:
        return None
    prev = u.scope.one("SELECT * FROM agent_runs WHERE tenant_id = :tenant_id AND status = 'succeeded' "
                       "AND as_of < :as_of ORDER BY as_of DESC LIMIT 1", as_of=run["as_of"])
    model = u.scope.one("SELECT * FROM model_versions WHERE tenant_id = :tenant_id AND status = 'active'")
    stale = next((s["stale_sources"] for s in json.loads(run["log"]) if s["step"] == "data_health"), {})
    return {"run": run, "prev": prev, "model": model, "params": json.loads(model["params"]),
            "validation": json.loads(model["metrics"])["validation"], "stale": stale}


SCORED = ("FROM scores s JOIN accounts a ON a.tenant_id = s.tenant_id AND a.account_id = s.account_id "
          "WHERE s.tenant_id = :tenant_id AND s.run_id = :run_id AND (:owner IS NULL OR a.csm_id = :owner) ")


# ------------------------------------------------------------------ pages

@app.get("/", response_class=HTMLResponse)
def my_week(request: Request, csm: str = "", u: User = Depends(current_user)):
    ctx = week_context(u)
    if ctx is None:
        return templates.TemplateResponse(request, "empty.html", {"user": u})
    owner = u.id if u.role == "csm" else (csm or None)     # leaders see everyone, or one CSM's book
    p = {"run_id": ctx["run"]["run_id"], "owner": owner}
    ranked = u.scope.all(
        "SELECT s.*, a.name, a.segment, a.csm_id, (SELECT x.p_lost FROM scores x WHERE x.tenant_id = s.tenant_id "
        "AND x.account_id = s.account_id AND x.run_id = :prev_run) AS prev_p "
        + SCORED + "AND s.blind_spot = 0 ORDER BY s.arr_at_risk DESC LIMIT 15",
        prev_run=ctx["prev"]["run_id"] if ctx["prev"] else -1, **p)
    blind = u.scope.all("SELECT s.*, a.name, a.csm_id " + SCORED + "AND s.blind_spot = 1 "
                        "AND julianday(s.renewal_date) - julianday(substr(s.as_of, 1, 10)) <= 120 "
                        "ORDER BY s.renewal_date LIMIT 15", **p)
    book = u.scope.one("SELECT COUNT(*) AS n, SUM(s.arr) AS arr, SUM(s.arr_at_risk) AS at_risk, "
                       "SUM(s.blind_spot) AS blind " + SCORED, **p)
    tasks = u.scope.all(
        "SELECT t.*, a.name, s.p_lost, s.arr, s.renewal_date FROM tasks t "
        "JOIN accounts a ON a.tenant_id = t.tenant_id AND a.account_id = t.account_id "
        "JOIN scores s ON s.score_id = t.score_id "
        "WHERE t.tenant_id = :tenant_id AND t.status = 'open' AND (:owner IS NULL OR t.csm_id = :owner) "
        "ORDER BY s.arr_at_risk DESC LIMIT 30", owner=owner)
    csms = u.scope.all("SELECT user_id, name FROM users WHERE tenant_id = :tenant_id AND role = 'csm' ORDER BY user_id")
    return templates.TemplateResponse(request, "my_week.html", {
        "user": u, **ctx, "owner": owner, "ranked": ranked, "blind": blind, "book": book, "tasks": tasks, "csms": csms})


@app.get("/accounts", response_class=HTMLResponse)
def accounts(request: Request, q: str = "", mine: int = 1, u: User = Depends(current_user)):
    ctx = week_context(u)
    if ctx is None:
        return templates.TemplateResponse(request, "empty.html", {"user": u})
    rows = u.scope.all(
        "SELECT s.*, a.name, a.segment, us.name AS csm_name FROM scores s "
        "JOIN accounts a ON a.tenant_id = s.tenant_id AND a.account_id = s.account_id "
        "LEFT JOIN users us ON us.user_id = a.csm_id "
        "WHERE s.tenant_id = :tenant_id AND s.run_id = :run_id AND (:owner IS NULL OR a.csm_id = :owner) "
        "AND (:q = '' OR a.name LIKE :like OR a.account_id LIKE :like) "
        "ORDER BY s.blind_spot, s.arr_at_risk DESC LIMIT 500",
        run_id=ctx["run"]["run_id"], owner=u.id if (mine and u.role == "csm") else None, q=q, like=f"%{q}%")
    return templates.TemplateResponse(request, "accounts.html", {"user": u, **ctx, "rows": rows, "q": q, "mine": mine})


def get_account(u: User, account_id):
    a = u.scope.one("SELECT a.*, us.name AS csm_name FROM accounts a LEFT JOIN users us ON us.user_id = a.csm_id "
                    "WHERE a.tenant_id = :tenant_id AND a.account_id = :account_id", account_id=account_id)
    if not a:
        raise HTTPException(404, "account not found")
    return a


@app.get("/accounts/{account_id}", response_class=HTMLResponse)
def account(request: Request, account_id: str, score_id: int | None = None, u: User = Depends(current_user)):
    a = get_account(u, account_id)
    by_account = {"account_id": account_id}
    history = u.scope.all("SELECT * FROM scores WHERE tenant_id = :tenant_id AND account_id = :account_id "
                          "ORDER BY as_of DESC", **by_account)
    s = history[0] if history else None
    if score_id is not None:
        s = next((h for h in history if h["score_id"] == score_id), None)
        if s is None:
            raise HTTPException(404, "score not found")
    model = params = logit = None
    if s:
        model = u.scope.one("SELECT * FROM model_versions WHERE tenant_id = :tenant_id AND model_id = :m",
                            m=s["model_id"])
        params = json.loads(model["params"])
        logit = params["intercept"] + sum(json.loads(s["contributions"]).values())
    tasks = u.scope.all("SELECT * FROM tasks WHERE tenant_id = :tenant_id AND account_id = :account_id "
                        "ORDER BY created_at DESC", **by_account)
    sent = u.scope.all("SELECT o.* FROM outbox o JOIN tasks t ON t.task_id = o.task_id "
                       "WHERE o.tenant_id = :tenant_id AND t.account_id = :account_id ORDER BY o.sent_at DESC", **by_account)
    renewals = u.scope.all("SELECT * FROM events WHERE tenant_id = :tenant_id AND account_id = :account_id "
                           "AND event_type IN ('renewal_won', 'renewal_lost') ORDER BY occurred_at DESC", **by_account)
    sources = u.scope.all("SELECT source, COUNT(*) AS n, MAX(occurred_at) AS last FROM events "
                          "WHERE tenant_id = :tenant_id AND account_id = :account_id GROUP BY source", **by_account)
    return templates.TemplateResponse(request, "account.html", {
        "user": u, "a": a, "s": s, "history": history, "model": model, "params": params, "logit": logit,
        "tasks": tasks, "sent": sent, "renewals": renewals, "sources": sources})


@app.get("/accounts/{account_id}/evidence", response_class=HTMLResponse)
def evidence(request: Request, account_id: str, feature: str, score_id: int, u: User = Depends(current_user)):
    """The raw events behind one input, as the score saw them, plus any that arrived too late to count."""
    a = get_account(u, account_id)
    s = u.scope.one("SELECT * FROM scores WHERE tenant_id = :tenant_id AND account_id = :account_id "
                    "AND score_id = :score_id", account_id=account_id, score_id=score_id)
    f = FEATURE_BY_NAME.get(feature)
    if not s or not f or not f.event_types:
        raise HTTPException(404, "no evidence for this input")

    def days_before(n):
        return conn().execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ', ?, ?)", (s["as_of"], f"-{n} days")).fetchone()[0]

    start = "0000" if f.window_days is None else days_before(f.window_days)
    end = days_before(f.skip_recent_days) if f.skip_recent_days else s["as_of"]
    types = {f"t{i}": t for i, t in enumerate(f.event_types)}
    sql = ("SELECT e.*, r.payload FROM events e JOIN raw_events r ON r.raw_id = e.raw_id "
           "WHERE e.tenant_id = :tenant_id AND e.account_id = :account_id "
           f"AND e.event_type IN ({', '.join(':' + k for k in types)}) "
           "AND e.occurred_at > :start AND e.occurred_at <= :end AND e.ingested_at {arrived} :as_of "
           "ORDER BY e.occurred_at DESC LIMIT 300")
    params = {"account_id": account_id, "start": start, "end": end, "as_of": s["as_of"], **types}
    return templates.TemplateResponse(request, "evidence.html", {
        "user": u, "a": a, "s": s, "f": f, "start": start, "end": end,
        "value": json.loads(s["features"]).get(feature),
        "used": u.scope.all(sql.format(arrived="<="), **params),
        "late": u.scope.all(sql.format(arrived=">"), **params)})


@app.post("/tasks/{task_id}/{action}")
def act_on_task(task_id: int, action: str, subject: str = Form(""), body: str = Form(""),
                u: User = Depends(current_user)):
    t = u.scope.one("SELECT * FROM tasks WHERE tenant_id = :tenant_id AND task_id = :task_id", task_id=task_id)
    if not t:
        raise HTTPException(404, "task not found")
    if u.role != "leader" and t["csm_id"] != u.id:
        raise HTTPException(403, "only the account's CSM can act on this task")
    if action not in ("send", "dismiss") or (action == "send" and not t["draft_to"]):
        raise HTTPException(400, "unknown action")
    with db.transaction(conn()):
        # Compare-and-set on status: a double click or a second tab can't send twice.
        changed = u.scope.all(
            "UPDATE tasks SET status = :status, resolved_at = :now, resolved_by = :by "
            "WHERE tenant_id = :tenant_id AND task_id = :task_id AND status = 'open' RETURNING task_id",
            status="sent" if action == "send" else "dismissed", now=now_iso(), by=u.id, task_id=task_id)
        if changed and action == "send":
            u.scope.execute(
                "INSERT INTO outbox (tenant_id, task_id, to_email, subject, body, sent_by, sent_at) "
                "VALUES (:tenant_id, :task_id, :to, :subject, :body, :by, :now)",
                task_id=task_id, to=t["draft_to"], subject=subject or t["draft_subject"],
                body=(body or t["draft_body"]).replace("{csm}", u.name), by=u.id, now=now_iso())
    return RedirectResponse(f"/accounts/{t['account_id']}", status_code=303)


@app.get("/runs", response_class=HTMLResponse)
def runs(request: Request, u: User = Depends(current_user)):
    rows = u.scope.all("SELECT * FROM agent_runs WHERE tenant_id = :tenant_id ORDER BY run_id DESC LIMIT 50")
    return templates.TemplateResponse(request, "runs.html", {"user": u, "runs": rows, "clock": db.get_meta(conn(), "clock")})


@app.get("/model", response_class=HTMLResponse)
def model_card(request: Request, u: User = Depends(current_user)):
    models = u.scope.all("SELECT * FROM model_versions WHERE tenant_id = :tenant_id ORDER BY trained_at DESC, model_id DESC")
    return templates.TemplateResponse(request, "model.html", {
        "user": u, "models": models, "features": FEATURE_BY_NAME, "missing_labels": MISSING_LABELS})


@app.get("/data", response_class=HTMLResponse)
def data_health(request: Request, u: User = Depends(current_user)):
    ctx = week_context(u) or {}
    as_of = ctx["run"]["as_of"] if ctx else "9999"
    delivery = u.scope.all(
        "SELECT r.source, COUNT(*) AS delivered, SUM(r.ingested_at > :as_of) AS in_flight, "
        "SUM(r.processed = 1 AND e.raw_id IS NULL AND j.raw_id IS NULL) AS duplicates "
        "FROM raw_events r LEFT JOIN events e ON e.raw_id = r.raw_id LEFT JOIN rejected_events j ON j.raw_id = r.raw_id "
        "WHERE r.tenant_id = :tenant_id GROUP BY r.source ORDER BY r.source", as_of=as_of)
    accepted = u.scope.all(
        "SELECT source, COUNT(*) AS n, COUNT(DISTINCT account_id) AS accounts, MIN(occurred_at) AS first, "
        "MAX(occurred_at) AS last, AVG(julianday(ingested_at) - julianday(occurred_at)) * 24 AS lag_h, "
        "SUM(julianday(ingested_at) - julianday(occurred_at) > 2) AS late "
        "FROM events WHERE tenant_id = :tenant_id GROUP BY source ORDER BY source")
    rejected = u.scope.all("SELECT source, reason, COUNT(*) AS n, MIN(detail) AS example FROM rejected_events "
                           "WHERE tenant_id = :tenant_id GROUP BY source, reason ORDER BY n DESC")
    crm = u.scope.one("SELECT COUNT(*) AS n, SUM(domain IS NULL) AS no_domain, SUM(arr IS NULL OR arr = 0) AS no_arr, "
                      "SUM(contact_email IS NULL) AS no_contact, SUM(workspace_id IS NULL) AS no_workspace "
                      "FROM accounts WHERE tenant_id = :tenant_id")
    return templates.TemplateResponse(request, "data.html", {
        "user": u, "delivery": delivery, "accepted": accepted, "rejected": rejected, "crm": crm,
        "stale": ctx.get("stale", {}), "run": ctx.get("run")})


# ------------------------------------------------------------------ scheduler hook and health

@app.post("/internal/agent/run")
def run_agent(authorization: str = Header("")):
    """Called by the scheduler (GitHub Actions), not by people."""
    if not hmac.compare_digest(authorization, f"Bearer {config.AGENT_TOKEN}"):
        raise HTTPException(401, "bad token")
    return {"results": agent.run_all(conn(), trigger="schedule")}


@app.get("/healthz")
def healthz():
    failed = conn().execute("SELECT COUNT(*) FROM agent_runs WHERE status = 'failed'").fetchone()[0]
    return {"ok": True, "clock": db.get_meta(conn(), "clock"), "failed_runs": failed}


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if exc.status_code == 303:
        return RedirectResponse(exc.headers["Location"], status_code=303)
    if request.url.path.startswith("/internal") or request.url.path == "/healthz":
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return templates.TemplateResponse(request, "error.html", {"code": exc.status_code, "detail": exc.detail},
                                      status_code=exc.status_code)
