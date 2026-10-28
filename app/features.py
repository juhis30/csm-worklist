"""Point-in-time account features.

build_features(events, accounts, as_of) is the single place features are
computed: for historical training snapshots and for this week's live scores
alike. It only counts events that had both happened AND reached us by as_of
(occurred_at <= as_of and ingested_at <= as_of), so a snapshot never contains
information that arrived later. Without the second condition, backtests would
quietly use late data the live system never had.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

YEAR = pd.Timedelta(days=365)


@dataclass(frozen=True)
class Feature:
    name: str
    label: str              # what a CSM reads
    source: str             # where the evidence lives
    event_types: tuple      # which events it counts (drives the evidence drill-down)
    window_days: int | None
    log: bool = False       # log1p before scaling: counts are heavy-tailed
    skip_recent_days: int = 0
    fmt: str = "{:.0f}"


FEATURES = [
    Feature("usage_30d", "Product events, last 30 days", "product", ("usage",), 30, log=True),
    Feature("usage_prior_60d", "Product events, 31-90 days ago", "product", ("usage",), 90, log=True, skip_recent_days=30),
    Feature("active_users_30d", "Active users, last 30 days", "product", ("usage",), 30, log=True),
    Feature("days_since_usage", "Days since last product activity", "product", ("usage",), None),
    Feature("tickets_90d", "Support tickets opened, last 90 days", "zendesk", ("ticket_opened",), 90, log=True),
    Feature("high_pri_tickets_90d", "High/urgent tickets, last 90 days", "zendesk", ("ticket_opened",), 90, log=True),
    Feature("open_tickets", "Tickets still open", "zendesk", ("ticket_opened", "ticket_solved"), 180),
    Feature("csat_bad_rate_180d", "Share of bad CSAT ratings, last 180 days", "zendesk", ("csat",), 180, fmt="{:.0%}"),
    Feature("payment_failures_120d", "Failed payments, last 120 days", "stripe", ("payment_failed",), 120),
    Feature("downgrades_365d", "Plan downgrades, last 365 days", "stripe", ("downgrade",), 365),
    Feature("reply_rate_60d", "Customer replies per CSM email, last 60 days", "gmail", ("email_in", "email_out"), 60, fmt="{:.2f}"),
    Feature("bounces_180d", "Bounced emails to contacts, last 180 days", "gmail", ("email_bounce",), 180),
    Feature("days_to_renewal", "Days until renewal", "salesforce", (), None),
    Feature("tenure_years", "Customer tenure (years)", "salesforce", (), None, fmt="{:.1f}"),
    Feature("log_arr", "ARR (log)", "salesforce", (), None, fmt="{:.2f}"),
]
FEATURE_BY_NAME = {f.name: f for f in FEATURES}

# Missing source -> its features are NaN, plus one yes/no indicator input.
# The model learns what "we have no data" means instead of reading it as healthy.
MISSING_GROUPS = {
    "no_product_data": ["usage_30d", "usage_prior_60d", "active_users_30d", "days_since_usage"],
    "no_csat": ["csat_bad_rate_180d"],
    "no_billing_data": ["payment_failures_120d", "downgrades_365d"],
    "no_email_data": ["reply_rate_60d", "bounces_180d"],
    "no_arr": ["log_arr"],
}
MISSING_LABELS = {
    "no_product_data": "No product usage data at all",
    "no_csat": "No CSAT ratings in 180 days",
    "no_billing_data": "No billing data (invoiced outside Stripe)",
    "no_email_data": "No email history",
    "no_arr": "ARR missing in Salesforce",
}
SIGNAL_SOURCES = ("product", "zendesk", "stripe", "gmail")


def load_events(conn, tenant_id) -> pd.DataFrame:
    df = pd.read_sql_query("SELECT source, account_id, event_type, occurred_at, ingested_at, value, actor, ref "
                           "FROM events WHERE tenant_id = ?", conn, params=(tenant_id,))
    df["occurred_at"] = pd.to_datetime(df["occurred_at"], utc=True)
    df["ingested_at"] = pd.to_datetime(df["ingested_at"], utc=True)
    return df


def load_accounts(conn, tenant_id) -> pd.DataFrame:
    df = pd.read_sql_query("SELECT * FROM accounts WHERE tenant_id = ?", conn, params=(tenant_id,))
    df["contract_start"] = pd.to_datetime(df["contract_start"], utc=True)
    return df.set_index("account_id")


def next_renewal(contract_start: pd.Series, as_of: pd.Timestamp) -> pd.Series:
    """First contract anniversary strictly after as_of (365-day contract years)."""
    years = np.floor((as_of - contract_start) / YEAR).astype(int) + 1
    return contract_start + pd.to_timedelta(years * 365, unit="D")


def visible(events: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    return events[(events["occurred_at"] <= as_of) & (events["ingested_at"] <= as_of)]


def active_accounts(accounts, ev_visible, as_of):
    """Customers at as_of whose renewal isn't already known to be lost."""
    started = accounts[accounts["contract_start"] <= as_of]
    lost = set(ev_visible.loc[ev_visible["event_type"] == "renewal_lost", "account_id"])
    return started[~started.index.isin(lost)]


def corroboration_required(tenant_config: dict) -> int:
    """How many non-product sources an account without usage data needs.

    Two normally. One if the organisation's own setup means fewer sources
    exist: a source never connected, or rolled out to only part of the base.
    Otherwise accounts are blind spots because of how the customer is set
    up, not because of anything about the account.
    """
    missing_by_design = (not all(tenant_config["integrations"].values())) or bool(tenant_config.get("partial_sources"))
    return 1 if missing_by_design else 2


def build_features(events: pd.DataFrame, accounts: pd.DataFrame, as_of, gmail_enabled=True,
                   min_corroboration=2) -> pd.DataFrame:
    as_of = pd.Timestamp(as_of)
    ev = visible(events, as_of)
    acc = active_accounts(accounts, ev, as_of)
    ev = ev[ev["account_id"].isin(acc.index)]
    idx = acc.index
    out = pd.DataFrame(index=idx)

    def between(kinds, start_days, end_days=0):
        """Events of these types with as_of - start_days < occurred_at <= as_of - end_days."""
        kinds = kinds if isinstance(kinds, tuple) else (kinds,)
        t = ev["occurred_at"]
        return ev[ev["event_type"].isin(kinds) & (t > as_of - pd.Timedelta(days=start_days))
                  & (t <= as_of - pd.Timedelta(days=end_days))]

    def count(frame):
        return frame.groupby("account_id").size().reindex(idx, fill_value=0).astype(float)

    # --- product usage: two levels, not a ratio ---
    out["usage_30d"] = count(between("usage", 30))
    out["usage_prior_60d"] = count(between("usage", 90, 30))
    out["active_users_30d"] = between("usage", 30).groupby("account_id")["actor"].nunique().reindex(idx, fill_value=0)
    last_use = ev[ev["event_type"] == "usage"].groupby("account_id")["occurred_at"].max().reindex(idx)
    out["days_since_usage"] = (as_of - last_use).dt.days.clip(upper=180).astype(float)
    has_product = last_use.notna()

    # --- support ---
    opened = between("ticket_opened", 90)
    out["tickets_90d"] = count(opened)
    out["high_pri_tickets_90d"] = count(opened[opened["value"] >= 3])
    opened_180 = between("ticket_opened", 180)
    solved = set(ev.loc[ev["event_type"] == "ticket_solved", "ref"])
    out["open_tickets"] = count(opened_180[~opened_180["ref"].isin(solved)])
    out["csat_bad_rate_180d"] = (1 - between("csat", 180).groupby("account_id")["value"].mean()).reindex(idx)

    # --- billing ---
    has_billing = pd.Series(idx.isin(ev.loc[ev["source"] == "stripe", "account_id"]), index=idx)
    out["payment_failures_120d"] = count(between("payment_failed", 120))
    out["downgrades_365d"] = count(between("downgrade", 365))

    # --- email metadata ---
    sent = count(between("email_out", 60))
    out["reply_rate_60d"] = (count(between("email_in", 60)) / sent.replace(0, np.nan)).clip(upper=1.5)
    out["bounces_180d"] = count(between("email_bounce", 180))
    has_email = pd.Series(gmail_enabled and idx.isin(ev.loc[ev["source"] == "gmail", "account_id"]), index=idx)

    # --- contract (Salesforce) ---
    renewal = next_renewal(acc["contract_start"], as_of)
    out["renewal_date"] = renewal.dt.strftime("%Y-%m-%d")
    out["days_to_renewal"] = (renewal - as_of).dt.days.astype(float)
    out["tenure_years"] = (as_of - acc["contract_start"]).dt.days / 365
    out["arr"] = acc["arr"].astype(float)
    out["log_arr"] = np.log1p(out["arr"].where(out["arr"] > 0))

    # Never heard from a source = unknown. Heard before, quiet now = a real zero.
    for has, group in ((has_product, "no_product_data"), (has_billing, "no_billing_data"), (has_email, "no_email_data")):
        out.loc[~has, MISSING_GROUPS[group]] = np.nan
    # The indicators are explicit columns, not "is the first feature empty": reply_rate
    # is also empty when the CSM simply sent nothing for 60 days, which isn't "no data".
    out["no_product_data"] = (~has_product).astype(float)
    out["no_billing_data"] = (~has_billing).astype(float)
    out["no_email_data"] = (~has_email).astype(float)
    out["no_csat"] = out["csat_bad_rate_180d"].isna().astype(float)
    out["no_arr"] = out["log_arr"].isna().astype(float)

    # Recent evidence per source: shown to CSMs and used by the blind-spot rule.
    recent = ev[ev["occurred_at"] > as_of - pd.Timedelta(days=180)]
    for s in SIGNAL_SOURCES:
        out[f"n_{s}_180d"] = count(recent[recent["source"] == s]).astype(int)
    out["csm_id"] = acc["csm_id"]
    out["blind_spot"] = [is_blind_spot(r, min_corroboration) for _, r in out.iterrows()]
    return out


def is_blind_spot(row, min_corroboration=2) -> bool:
    """Too little data to rank fairly with the rest.

    Usage is the main signal. With no usage history at all, we need recent
    evidence from other sources (see corroboration_required) before trusting the score.
    """
    if not np.isnan(row["usage_30d"]):
        return False
    return sum(row[f"n_{s}_180d"] > 0 for s in ("zendesk", "stripe", "gmail")) < min_corroboration
