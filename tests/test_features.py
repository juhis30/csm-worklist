import numpy as np
import pandas as pd

from app.features import build_features, is_blind_spot, next_renewal

AS_OF = pd.Timestamp("2026-06-01T06:00:00Z")


def ts(s):
    return pd.Timestamp(s, tz="UTC")


def accounts():
    return pd.DataFrame({"account_id": ["A1", "A2"], "contract_start": [ts("2024-09-01"), ts("2025-01-15")],
                         "arr": [10_000.0, None], "csm_id": ["c1", "c1"]}).set_index("account_id")


def events(rows):
    df = pd.DataFrame(rows, columns=["source", "account_id", "event_type", "occurred_at", "ingested_at",
                                     "value", "actor", "ref"])
    df["occurred_at"] = pd.to_datetime(df["occurred_at"], utc=True)
    df["ingested_at"] = pd.to_datetime(df["ingested_at"], utc=True)
    return df


def usage(occurred, ingested, user="u1"):
    return ("product", "A1", "usage", occurred, ingested, None, user, None)


def test_events_that_arrived_after_as_of_are_invisible():
    ev = events([usage("2026-05-20", "2026-05-20"),
                 usage("2026-05-25", "2026-06-10", "u2"),     # happened in window, reached us later
                 usage("2026-06-05", "2026-06-05", "u3")])    # in the future
    f = build_features(ev, accounts(), AS_OF)
    assert f.loc["A1", "usage_30d"] == 1 and f.loc["A1", "active_users_30d"] == 1
    assert build_features(ev, accounts(), ts("2026-06-11")).loc["A1", "usage_30d"] == 3


def test_usage_levels_use_separate_windows():
    ev = events([usage("2026-05-20", "2026-05-20"), usage("2026-04-01", "2026-04-01"), usage("2026-01-01", "2026-01-01")])
    f = build_features(ev, accounts(), AS_OF)
    assert f.loc["A1", "usage_30d"] == 1 and f.loc["A1", "usage_prior_60d"] == 1


def test_never_had_data_is_missing_but_went_quiet_is_zero():
    ev = events([usage("2026-01-01", "2026-01-01")])
    f = build_features(ev, accounts(), AS_OF)
    assert f.loc["A1", "usage_30d"] == 0 and f.loc["A1", "days_since_usage"] == 151
    assert np.isnan(f.loc["A2", "usage_30d"]) and np.isnan(f.loc["A2", "payment_failures_120d"])
    assert np.isnan(f.loc["A2", "log_arr"])   # ARR missing in CRM


def test_quiet_inbox_is_not_the_same_as_no_email_data():
    ev = events([("gmail", "A1", "email_out", "2026-01-10", "2026-01-10", None, None, None)])   # history, but not recent
    f = build_features(ev, accounts(), AS_OF)
    assert np.isnan(f.loc["A1", "reply_rate_60d"])        # nothing sent in 60 days: no rate to compute
    assert f.loc["A1", "no_email_data"] == 0              # ...but we do have email history
    assert f.loc["A2", "no_email_data"] == 1


def test_gmail_not_connected_means_email_features_missing():
    ev = events([("gmail", "A1", "email_out", "2026-05-20", "2026-05-20", None, None, None)])
    assert np.isnan(build_features(ev, accounts(), AS_OF, gmail_enabled=False).loc["A1", "reply_rate_60d"])
    assert build_features(ev, accounts(), AS_OF, gmail_enabled=True).loc["A1", "reply_rate_60d"] == 0


def test_lost_account_drops_out_only_once_we_know():
    ev = events([("salesforce", "A1", "renewal_lost", "2026-05-01", "2026-05-02", 1, None, "2026-09-01")])
    assert "A1" not in build_features(ev, accounts(), AS_OF).index
    assert "A1" in build_features(ev, accounts(), ts("2026-05-01T12:00:00")).index


def test_open_tickets_and_priority():
    ev = events([("zendesk", "A1", "ticket_opened", "2026-05-01", "2026-05-01", 4, None, "t1"),
                 ("zendesk", "A1", "ticket_opened", "2026-05-02", "2026-05-02", 2, None, "t2"),
                 ("zendesk", "A1", "ticket_solved", "2026-05-03", "2026-05-03", None, None, "t2")])
    f = build_features(ev, accounts(), AS_OF)
    assert f.loc["A1", "open_tickets"] == 1 and f.loc["A1", "high_pri_tickets_90d"] == 1


def test_next_renewal_is_strictly_after_as_of():
    starts = pd.Series([ts("2024-09-01"), AS_OF])
    assert list(next_renewal(starts, AS_OF).dt.strftime("%Y-%m-%d")) == ["2026-09-01", "2027-06-01"]


def test_blind_spot_rule():
    row = {"usage_30d": np.nan, "n_zendesk_180d": 3, "n_stripe_180d": 0, "n_gmail_180d": 0}
    assert is_blind_spot(row)
    assert not is_blind_spot({**row, "n_gmail_180d": 2})       # two corroborating sources
    assert not is_blind_spot({**row, "usage_30d": 0.0})        # usage history exists, even if quiet now
