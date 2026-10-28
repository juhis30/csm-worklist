import json
import math

import numpy as np
import pandas as pd
import pytest
from sklearn.linear_model import LogisticRegression

from app.features import FEATURES, MISSING_GROUPS
from app.model import LinearModel, ranking_metrics, score, validation_verdict


@pytest.fixture
def frame():
    rng = np.random.default_rng(0)
    n = 600
    df = pd.DataFrame({f.name: rng.poisson(3, n).astype(float) for f in FEATURES})
    for flag in MISSING_GROUPS:
        df[flag] = 0.0
    df.loc[:50, "usage_30d"] = np.nan                 # some gaps, as in real data
    df.loc[:50, "no_product_data"] = 1.0
    df["arr"] = rng.uniform(5_000, 50_000, n)
    risk = -1.5 - 0.6 * np.log1p(df["usage_30d"].fillna(0)) + 0.4 * df["tickets_90d"]
    df["label"] = (rng.random(n) < 1 / (1 + np.exp(-risk))).astype(int)
    df["snapshot"] = pd.Timestamp("2026-01-05", tz="UTC")
    return df


def test_contributions_add_up_to_the_score(frame):
    m = LinearModel.fit(frame)
    out = score(m, frame)
    for _, r in out.head(20).iterrows():
        logit = m.p["intercept"] + sum(json.loads(r["contributions"]).values())
        assert math.isclose(1 / (1 + math.exp(-logit)), r["p_lost"], abs_tol=1e-6)


def test_hand_written_scoring_matches_sklearn(frame):
    m = LinearModel.fit(frame)
    X = LinearModel.raw_inputs(frame, m.p["medians"])
    Z = (X - pd.Series(m.p["means"])) / pd.Series(m.p["stds"])
    ref = LogisticRegression(C=0.3, max_iter=2000).fit(Z, frame["label"]).predict_proba(Z)[:, 1]
    assert np.allclose(m.predict(frame), ref, atol=1e-6)


def test_model_learns_the_planted_signal(frame):
    coefs = LinearModel.fit(frame).p["coefs"]
    assert coefs["usage_30d"] < 0 < coefs["tickets_90d"]


def test_model_survives_json_roundtrip(frame):
    m = LinearModel.fit(frame)
    again = LinearModel(json.loads(json.dumps(m.p)))
    assert np.allclose(m.predict(frame), again.predict(frame))


def test_perfect_ranking_has_full_precision(frame):
    m = ranking_metrics(frame, frame["label"].astype(float).to_numpy())
    assert m["precision_top_decile"] == 1.0


def test_validation_flags_a_model_that_doesnt_beat_the_rule():
    weak = {"test_positives": 90, "model": {"auc": 0.58, "lift_top_decile": 1.1},
            "rules_baseline": {"auc": 0.57, "lift_top_decile": 2.0}}
    verdict = validation_verdict(weak)
    assert not verdict["validated"] and len(verdict["reasons"]) == 3
    strong = {"test_positives": 300, "model": {"auc": 0.67, "lift_top_decile": 2.6},
              "rules_baseline": {"auc": 0.53, "lift_top_decile": 1.7}}
    assert validation_verdict(strong)["validated"]
