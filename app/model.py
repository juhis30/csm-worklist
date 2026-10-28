"""Renewal-loss model: labels, out-of-time evaluation, scoring, explanation.

Predicts P(the account's next renewal is lost) from point-in-time features,
one model per organisation, trained only on that organisation's data. Labels
come from Salesforce closed-renewal events, the system's own data.

Why logistic regression: on standardised inputs the log-odds is an exact sum
of per-input contributions, so "why is this account at the top" has a precise
answer that adds up to the number shown. The fitted model is stored as plain
JSON (medians, means, stds, coefficients), and scoring is a dot product
written out below, so any score can be recomputed by hand.
"""

import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score

from app.features import FEATURES, MISSING_GROUPS, build_features

SNAPSHOT_EVERY = pd.Timedelta(days=28)
HOLDOUT = pd.Timedelta(days=180)
WARMUP = pd.Timedelta(days=120)       # history needed before the first snapshot
MIN_TRAIN_POSITIVES = 40
MIN_TEST_POSITIVES = 10
INPUTS = [f.name for f in FEATURES] + list(MISSING_GROUPS)


# ------------------------------------------------------------------ labelled snapshots

def renewal_outcomes(events, known_by):
    """(account_id, renewal_date) -> lost?, and when we found out. Only outcomes known by known_by."""
    r = events[events["event_type"].isin(["renewal_won", "renewal_lost"]) & (events["ingested_at"] <= known_by)]
    r = r.assign(lost=(r["event_type"] == "renewal_lost").astype(int),
                 known_at=r[["occurred_at", "ingested_at"]].max(axis=1))
    return r.drop_duplicates(["account_id", "ref"]).set_index(["account_id", "ref"])[["lost", "known_at"]]


def training_frame(events, accounts, cutoff, gmail_enabled=True):
    """Every active account every 4 weeks, labelled with how its next renewal went.

    A row is kept only if the outcome was known by the cutoff (otherwise there's
    no label) and NOT yet known at the snapshot (otherwise there's nothing left
    to predict: e.g. the customer had already given notice).
    """
    cutoff = pd.Timestamp(cutoff)
    outcomes = renewal_outcomes(events, cutoff)
    frames, t = [], cutoff - SNAPSHOT_EVERY
    while t >= events["occurred_at"].min() + WARMUP:
        f = build_features(events, accounts, t, gmail_enabled)
        o = outcomes.reindex(list(zip(f.index, f["renewal_date"])))
        f = f.assign(label=o["lost"].to_numpy(), known_at=o["known_at"].to_numpy(), snapshot=t)
        frames.append(f[f["label"].notna() & (f["known_at"] > t)])
        t -= SNAPSHOT_EVERY
    df = pd.concat(frames)
    df["label"] = df["label"].astype(int)
    return df


# ------------------------------------------------------------------ the model, as data

class LinearModel:
    def __init__(self, params):
        self.p = params

    @staticmethod
    def raw_inputs(df, medians):
        """Features -> model inputs before scaling: log counts, fill gaps with the training median."""
        X = pd.DataFrame(index=df.index)
        for f in FEATURES:
            col = df[f.name].astype(float)
            if f.log:
                col = np.log1p(col.clip(lower=0))
            X[f.name] = col.fillna(medians[f.name])
        for flag in MISSING_GROUPS:
            X[flag] = df[flag].astype(float)
        return X

    @classmethod
    def fit(cls, df):
        medians = {}
        for f in FEATURES:
            col = df[f.name].astype(float)
            col = np.log1p(col.clip(lower=0)) if f.log else col
            medians[f.name] = float(col.median()) if col.notna().any() else 0.0
        X = cls.raw_inputs(df, medians)
        means, stds = X.mean(), X.std().replace(0, 1.0)
        clf = LogisticRegression(C=0.3, max_iter=2000).fit((X - means) / stds, df["label"])
        return cls({"inputs": INPUTS, "medians": medians, "means": means.to_dict(), "stds": stds.to_dict(),
                    "coefs": dict(zip(INPUTS, clf.coef_[0].tolist())), "intercept": float(clf.intercept_[0])})

    def contributions(self, df):
        """Per-input log-odds contribution, relative to an average account."""
        X = self.raw_inputs(df, self.p["medians"])
        Z = (X - pd.Series(self.p["means"])) / pd.Series(self.p["stds"])
        return Z[self.p["inputs"]] * pd.Series(self.p["coefs"])

    def predict(self, df):
        return 1 / (1 + np.exp(-(self.p["intercept"] + self.contributions(df).sum(axis=1))))


def rules_baseline(df):
    """What a sensible CSM spreadsheet might do: usage down, tickets up, payment trouble, bounces."""
    usage_drop = np.log1p(df["usage_prior_60d"] / 2) - np.log1p(df["usage_30d"])
    return (usage_drop.fillna(0) + 0.5 * df["high_pri_tickets_90d"] + 0.3 * df["open_tickets"]
            + df["payment_failures_120d"].fillna(0) + df["bounces_180d"].fillna(0))


# ------------------------------------------------------------------ evaluation

def ranking_metrics(df, score):
    """How the ranking would feel in use, measured on each Monday separately.

    precision_top_decile: of the 10% we'd rank highest, the share actually lost.
    lift_top_decile:      that, divided by the base rate (1.0 = no better than random).
    lost_arr_top20:       of all ARR eventually lost, the share sitting in the
                          top 20% by expected ARR at risk (p x ARR): our actual ranking.
    """
    d = df.assign(s=score, ear=score * df["arr"].fillna(0))
    precisions, caught, total = [], 0.0, 0.0
    for _, monday in d.groupby("snapshot"):
        if len(monday) < 20:
            continue
        precisions.append(monday.nlargest(max(1, len(monday) // 10), "s")["label"].mean())
        top = monday.nlargest(max(1, len(monday) // 5), "ear")
        total += monday.loc[monday["label"] == 1, "arr"].fillna(0).sum()
        caught += top.loc[top["label"] == 1, "arr"].fillna(0).sum()
    precision, base = float(np.mean(precisions)), float(d["label"].mean())
    return {"precision_top_decile": precision, "lift_top_decile": precision / base,
            "lost_arr_top20": caught / total if total else None}


def evaluate(y, p, df):
    return {"auc": float(roc_auc_score(y, p)), "pr_auc": float(average_precision_score(y, p)), **ranking_metrics(df, p)}


def calibration_table(y, p, bins=10):
    """Within each tenth of predictions: predicted vs actual loss rate."""
    t = pd.DataFrame({"y": y, "p": p, "bin": pd.qcut(p, bins, labels=False, duplicates="drop")})
    t = t.groupby("bin").agg(predicted=("p", "mean"), observed=("y", "mean"), n=("y", "size"))
    return t.round(4).reset_index(drop=True).to_dict(orient="records")


def validation_verdict(m):
    """Is there enough held-out evidence that the model beats the simple rule?

    Not a blocker: scores still come from the model. CSMs are told plainly
    when the ranking is only directional, and why.
    """
    lin, rules, reasons = m["model"], m["rules_baseline"], []
    if m["test_positives"] < 100:
        reasons.append(f"only {m['test_positives']} lost renewals to test on")
    if lin["auc"] < rules["auc"] + 0.02:
        reasons.append(f"AUC {lin['auc']:.2f} is not clearly above the simple rule's {rules['auc']:.2f}")
    if lin["lift_top_decile"] < max(1.5, rules["lift_top_decile"]):
        reasons.append(f"top-10% lift {lin['lift_top_decile']:.1f}x doesn't beat the simple rule's "
                       f"{rules['lift_top_decile']:.1f}x")
    return {"validated": not reasons, "reasons": reasons}


def train_and_evaluate(events, accounts, cutoff, gmail_enabled=True):
    """Out-of-time test, then refit on everything known by the cutoff.

    Train: snapshots before cutoff - 180 days whose outcome was already known
    by then. Test: snapshots in the last 180 days. Nothing from the test
    period reaches training, not even through labels.
    """
    cutoff = pd.Timestamp(cutoff)
    df = training_frame(events, accounts, cutoff, gmail_enabled)
    split = cutoff - HOLDOUT
    train = df[(df["snapshot"] < split) & (df["known_at"] <= split)]
    test = df[df["snapshot"] >= split]
    if train["label"].sum() < MIN_TRAIN_POSITIVES or test["label"].sum() < MIN_TEST_POSITIVES:
        return None, {"error": f"not enough renewal outcomes: train={int(train['label'].sum())}, "
                               f"test={int(test['label'].sum())} lost"}

    p = LinearModel.fit(train).predict(test).to_numpy()
    y = test["label"].to_numpy()
    metrics = {
        "cutoff": cutoff.isoformat(), "split": split.isoformat(),
        "train_rows": len(train), "train_positives": int(train["label"].sum()),
        "test_rows": len(test), "test_positives": int(y.sum()), "base_rate": float(y.mean()),
        "model": {**evaluate(y, p, test), "brier": float(brier_score_loss(y, p))},
        "rules_baseline": evaluate(y, rules_baseline(test).to_numpy(), test),
        "calibration": calibration_table(y, p),
    }
    metrics["validation"] = validation_verdict(metrics)

    final = LinearModel.fit(df)              # deploy a model that has seen the latest outcomes
    final.p["trained_rows"] = len(df)
    final.p["base_rate"] = float(df["label"].mean())
    return final, metrics


# ------------------------------------------------------------------ persistence and scoring

def save_model(conn, tenant_id, model, metrics, trained_at, status="active", note=""):
    model_id = f"{tenant_id}-{trained_at[:10]}-{datetime.now(timezone.utc):%H%M%S%f}"
    conn.execute("INSERT INTO model_versions VALUES (?, ?, ?, 'logistic_regression', ?, ?, ?, ?)",
                 (model_id, tenant_id, trained_at, json.dumps(model.p), json.dumps(metrics), status, note))
    return model_id


def active_model(conn, tenant_id):
    row = conn.execute("SELECT * FROM model_versions WHERE tenant_id = ? AND status = 'active'", (tenant_id,)).fetchone()
    return (row, LinearModel(json.loads(row["params"]))) if row else (None, None)


def score(model: LinearModel, feats: pd.DataFrame) -> pd.DataFrame:
    contrib = model.contributions(feats)
    out = feats.copy()
    out["p_lost"] = 1 / (1 + np.exp(-(model.p["intercept"] + contrib.sum(axis=1))))
    out["arr_at_risk"] = out["p_lost"] * feats["arr"].fillna(0)
    # Unrounded, so stored contributions add up to the stored score exactly.
    out["contributions"] = [json.dumps(r) for r in contrib.to_dict("records")]
    return out
