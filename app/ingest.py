"""Landing zone -> normalised events.

Every source has its own timestamp format and its own way of pointing at an
account. This module turns them all into one row shape, matches the account,
drops re-deliveries (first delivery wins), and records a reason for anything
it can't use. Nothing disappears silently.

Rejections that depend on CRM data (unknown domain, unmapped workspace, ...)
are retried on every run, so fixing the CRM recovers the events. A recovered
event counts as ingested when it was recovered, because that's when the
system could first use it; backtests stay honest.
"""

import json
from collections import Counter
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

UTC = timezone.utc
PERSONAL_DOMAINS = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com"}
PRIORITY = {"low": 1, "normal": 2, "high": 3, "urgent": 4}
# Reasons a CRM fix could resolve. The others (bad data) are final.
RETRYABLE = {"unresolved_account", "unmapped_workspace", "personal_email", "ambiguous_domain"}


class Reject(Exception):
    def __init__(self, reason, detail=None):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


def normalize_domain(value):
    """'https://www.Acme.com/', 'WWW.ACME.COM', 'jo@acme.com' -> 'acme.com'."""
    if not value:
        return None
    d = value.strip().lower()
    if "@" in d:
        d = d.split("@", 1)[1]
    for prefix in ("https://", "http://"):
        d = d.removeprefix(prefix)
    d = d.split("/", 1)[0].removeprefix("www.")
    return d or None


def to_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def require(value, what="timestamp"):
    if value in (None, "", "N/A"):
        raise Reject("bad_timestamp", f"missing {what}")
    return value


class Resolver:
    """Maps each source's way of naming an account to a Salesforce account id."""

    def __init__(self, conn, tenant_id):
        self.domains, self.ambiguous, self.workspaces, self.contacts, self.ids = {}, set(), {}, {}, set()
        for r in conn.execute("SELECT account_id, domain, workspace_id, contact_email FROM accounts "
                              "WHERE tenant_id = ?", (tenant_id,)):
            self.ids.add(r["account_id"])
            if d := normalize_domain(r["domain"]):
                if d in self.domains:
                    self.ambiguous.add(d)       # two accounts claim one domain: refuse to guess
                self.domains[d] = r["account_id"]
            if r["workspace_id"]:
                self.workspaces[r["workspace_id"]] = r["account_id"]
            if r["contact_email"]:
                self.contacts[r["contact_email"].strip().lower()] = r["account_id"]

    def by_email_or_domain(self, value):
        if value and "@" in value and (exact := self.contacts.get(value.strip().lower())):
            return exact                        # a known contact, even on a personal address
        d = normalize_domain(value)
        if not d:
            raise Reject("unresolved_account", "no domain or email")
        if d in PERSONAL_DOMAINS:
            raise Reject("personal_email", value)
        if d in self.ambiguous:
            raise Reject("ambiguous_domain", d)
        if d not in self.domains:
            raise Reject("unresolved_account", d)
        return self.domains[d]

    def by_workspace(self, ws):
        if ws not in self.workspaces:
            raise Reject("unmapped_workspace", ws)
        return self.workspaces[ws]


# Each parser returns (account_id, event_type, occurred_at, value, actor, ref).

def parse_product(p, res):
    occurred = datetime.fromtimestamp(require(p.get("ts")) / 1000, UTC)       # epoch ms
    return res.by_workspace(p.get("workspace_id")), "usage", occurred, None, p.get("user_id"), p.get("event")


def parse_zendesk(p, res):
    occurred = datetime.fromisoformat(require(p.get("updated_at")).replace("Z", "+00:00"))
    t = p["ticket"]
    account = res.by_email_or_domain(t.get("organization"))
    kind = p["type"]
    if kind == "ticket.created":
        return account, "ticket_opened", occurred, PRIORITY.get(t.get("priority")), None, str(t["id"])
    if kind == "ticket.solved":
        return account, "ticket_solved", occurred, None, None, str(t["id"])
    if kind == "satisfaction.rated":
        return account, "csat", occurred, 1.0 if p.get("score") == "good" else 0.0, None, str(t["id"])
    raise Reject("unknown_type", kind)


def parse_stripe(p, res):
    occurred = datetime.fromtimestamp(require(p.get("created")), UTC)        # epoch seconds
    obj = p["data"]["object"]
    kind = {"invoice.paid": "invoice_paid", "invoice.payment_failed": "payment_failed",
            "customer.subscription.downgraded": "downgrade"}.get(p["type"])
    if not kind:
        raise Reject("unknown_type", p["type"])
    return res.by_email_or_domain(obj.get("customer_email")), kind, occurred, obj.get("amount", 0) / 100, None, None


def parse_gmail(p, res):
    occurred = parsedate_to_datetime(require(p.get("date")))                 # RFC 2822, any time zone
    counterparty = p["from"] if p["direction"] == "in" else p["to"]
    kind = {"in": "email_in", "out": "email_out", "bounce": "email_bounce"}[p["direction"]]
    return res.by_email_or_domain(counterparty), kind, occurred, None, None, None


def parse_salesforce(p, res):
    if p.get("AccountId") not in res.ids:
        raise Reject("unresolved_account", p.get("AccountId"))
    if p.get("Type") != "Renewal" or not p.get("StageName", "").startswith("Closed"):
        raise Reject("not_a_closed_renewal", p.get("StageName"))
    occurred = datetime.fromisoformat(require(p.get("CloseDate"), "CloseDate")).replace(tzinfo=UTC)
    kind = "renewal_won" if p["StageName"] == "Closed Won" else "renewal_lost"
    return p["AccountId"], kind, occurred, p.get("Amount"), None, p.get("Renewal_Date__c")


PARSERS = {"product": parse_product, "zendesk": parse_zendesk, "stripe": parse_stripe,
           "gmail": parse_gmail, "salesforce": parse_salesforce}


def _process(conn, tenant_id, res, row, available_at, stats, retry):
    source = row["source"]
    try:
        if source not in PARSERS:
            raise Reject("unknown_source", source)
        account, kind, occurred, value, actor, ref = PARSERS[source](json.loads(row["payload"]), res)
        occurred_iso = to_iso(occurred)
        # An event claiming to be from well after it reached us has a broken clock.
        if (occurred - datetime.fromisoformat(row["ingested_at"].replace("Z", "+00:00"))).total_seconds() > 86400:
            raise Reject("future_timestamp", occurred_iso)
        inserted = conn.execute(
            "INSERT OR IGNORE INTO events (tenant_id, source, source_event_id, account_id, event_type, "
            "occurred_at, ingested_at, value, actor, ref, raw_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tenant_id, source, row["source_event_id"], account, kind, occurred_iso, available_at,
             value, actor, ref, row["raw_id"])).rowcount
        if retry:
            conn.execute("DELETE FROM rejected_events WHERE raw_id = ?", (row["raw_id"],))
        stats[f"{source}.{'recovered' if retry else 'accepted' if inserted else 'duplicate'}"] += 1
    except Reject as e:
        if not retry:
            _reject(conn, tenant_id, row, e.reason, e.detail)
            stats[f"{source}.rejected.{e.reason}"] += 1
    except (ValueError, KeyError, TypeError, AttributeError) as e:   # payload not the shape we expect
        if not retry:
            _reject(conn, tenant_id, row, "malformed", repr(e))
            stats[f"{source}.rejected.malformed"] += 1


def _reject(conn, tenant_id, row, reason, detail):
    conn.execute("INSERT OR REPLACE INTO rejected_events VALUES (?, ?, ?, ?, ?, ?)",
                 (row["raw_id"], tenant_id, row["source"], reason, str(detail)[:200], row["ingested_at"]))


def ingest(conn, tenant_id, as_of_iso):
    """Process everything that had arrived by as_of, then retry recoverable rejections.

    Safe to re-run: processed rows are skipped and the events primary key
    ignores re-deliveries. Returns counts for the run log.
    """
    res = Resolver(conn, tenant_id)
    stats = Counter()
    new = conn.execute(
        "SELECT raw_id, source, source_event_id, payload, ingested_at FROM raw_events "
        "WHERE tenant_id = ? AND processed = 0 AND ingested_at <= ? ORDER BY ingested_at, raw_id",
        (tenant_id, as_of_iso)).fetchall()
    for row in new:
        _process(conn, tenant_id, res, row, row["ingested_at"], stats, retry=False)
    conn.executemany("UPDATE raw_events SET processed = 1 WHERE raw_id = ?", [(r["raw_id"],) for r in new])

    retryable = conn.execute(
        "SELECT r.raw_id, r.source, r.source_event_id, r.payload, r.ingested_at FROM rejected_events j "
        f"JOIN raw_events r USING (raw_id) WHERE j.tenant_id = ? AND j.reason IN ({','.join('?' * len(RETRYABLE))}) "
        "AND j.ingested_at < ?", (tenant_id, *sorted(RETRYABLE), as_of_iso)).fetchall()
    just_rejected = {n["raw_id"] for n in new}    # no point retrying what failed a moment ago
    retryable = [r for r in retryable if r["raw_id"] not in just_rejected]
    for row in retryable:
        _process(conn, tenant_id, res, row, as_of_iso, stats, retry=True)
    return dict(sorted(stats.items()))
