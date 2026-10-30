from app import outreach
from app.agent import strongest_driver

ACCOUNT = {"name": "Acme", "contact_name": "Jo Doe", "contact_email": "jo@acme.com"}


def test_missing_data_driver_never_claims_we_looked_at_usage():
    d = outreach.draft("entered_top10", "no_product_data", ACCOUNT, {"no_product_data": 1.0}, 120)
    assert "No product usage data" in d["reason"]
    assert "using the platform" not in d["body"] and d["subject"] == "Checking in"


def test_real_driver_picks_the_matching_play():
    d = outreach.draft("risk_jumped", "tickets_90d", ACCOUNT, {"tickets_90d": 4.0}, 60)
    assert "Support tickets opened, last 90 days = 4" in d["reason"]
    assert "support requests" in d["body"]


def test_no_contact_or_blind_spot_means_no_customer_email():
    assert outreach.draft("blind_spot", None, ACCOUNT, {}, 30)["to"] is None
    assert outreach.draft("risk_jumped", "tickets_90d", {**ACCOUNT, "contact_email": None}, {"tickets_90d": 2.0}, 30)["to"] is None


def test_strongest_driver_ignores_values_that_were_only_filled_in():
    contributions = {"usage_prior_60d": 0.30, "no_product_data": 0.20, "tickets_90d": 0.10}
    features = {"usage_prior_60d": None, "no_product_data": 1.0, "tickets_90d": 2.0}
    assert strongest_driver(contributions, features) == ("no_product_data", "missing")


def test_strongest_driver_ignores_no_data_inputs_when_the_data_exists():
    contributions = {"no_csat": 0.40, "tickets_90d": 0.10}
    features = {"no_csat": 0.0, "tickets_90d": 2.0}
    assert strongest_driver(contributions, features) == ("tickets_90d", "zendesk")
