-- Every business table carries tenant_id. The web app reads them only through
-- db.TenantScope, which refuses any query without a :tenant_id filter.

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tenants (
    tenant_id TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    config    TEXT NOT NULL            -- JSON: which integrations are connected
);

CREATE TABLE IF NOT EXISTS users (
    user_id   TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
    name      TEXT NOT NULL,
    email     TEXT NOT NULL,
    role      TEXT NOT NULL CHECK (role IN ('csm', 'leader'))
);

-- Account master as synced from Salesforce: current values, dirty as in real CRMs.
CREATE TABLE IF NOT EXISTS accounts (
    tenant_id      TEXT NOT NULL REFERENCES tenants(tenant_id),
    account_id     TEXT NOT NULL,
    name           TEXT NOT NULL,
    domain         TEXT,               -- as typed: may have https://, www., capitals, or be missing
    segment        TEXT,
    arr            REAL,               -- may be NULL or 0
    seats          INTEGER,
    csm_id         TEXT REFERENCES users(user_id),
    contract_start TEXT NOT NULL,      -- renewals fall on each anniversary
    contact_name   TEXT,
    contact_email  TEXT,
    workspace_id   TEXT,               -- product workspace, when someone mapped it
    PRIMARY KEY (tenant_id, account_id)
);

-- Layer 1: landing zone. Payloads exactly as each source delivered them.
-- A row becomes visible to the system only once the clock passes ingested_at.
CREATE TABLE IF NOT EXISTS raw_events (
    raw_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       TEXT NOT NULL,
    source          TEXT NOT NULL,
    source_event_id TEXT,
    payload         TEXT NOT NULL,     -- JSON, source-specific shape
    ingested_at     TEXT NOT NULL,     -- when it reached us
    processed       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_raw_pending ON raw_events (tenant_id, processed, ingested_at);

-- Layer 2: one shape for all sources, matched to an account, deduplicated.
CREATE TABLE IF NOT EXISTS events (
    tenant_id       TEXT NOT NULL,
    source          TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    account_id      TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    occurred_at     TEXT NOT NULL,     -- UTC ISO-8601: when it happened
    ingested_at     TEXT NOT NULL,     -- when we first learned about it
    value           REAL,              -- e.g. ticket priority, invoice amount, CSAT
    actor           TEXT,              -- e.g. product user id
    ref             TEXT,              -- e.g. ticket id, renewal date
    raw_id          INTEGER NOT NULL REFERENCES raw_events(raw_id),
    PRIMARY KEY (tenant_id, source, source_event_id)   -- a re-delivery can't insert twice
);
CREATE INDEX IF NOT EXISTS ix_events_account ON events (tenant_id, account_id, occurred_at);
-- Raw -> clean lookups (data health, evidence payloads). Without it, matching
-- every raw event to its clean row is a full scan per row.
CREATE INDEX IF NOT EXISTS ix_events_raw ON events (raw_id);

-- Layer 3: what we threw away, and why. Nothing disappears silently.
CREATE TABLE IF NOT EXISTS rejected_events (
    raw_id      INTEGER PRIMARY KEY REFERENCES raw_events(raw_id),
    tenant_id   TEXT NOT NULL,
    source      TEXT NOT NULL,
    reason      TEXT NOT NULL,
    detail      TEXT,
    ingested_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_versions (
    model_id   TEXT PRIMARY KEY,
    tenant_id  TEXT NOT NULL,
    trained_at TEXT NOT NULL,
    algorithm  TEXT NOT NULL,
    params     TEXT NOT NULL,          -- JSON: features, scaling, coefficients. The model itself.
    metrics    TEXT NOT NULL,          -- JSON: out-of-time evaluation
    status     TEXT NOT NULL CHECK (status IN ('active', 'retired', 'rejected')),
    note       TEXT
);

CREATE TABLE IF NOT EXISTS agent_runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id   TEXT NOT NULL,
    as_of       TEXT NOT NULL,
    trigger     TEXT NOT NULL,         -- bootstrap | schedule | manual
    status      TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    log         TEXT NOT NULL DEFAULT '[]',   -- JSON list of step records
    UNIQUE (tenant_id, as_of)                 -- one run per tenant per week
);

-- Account state is never updated in place. Each run writes a new snapshot,
-- with everything needed to explain the number later.
CREATE TABLE IF NOT EXISTS scores (
    score_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id     TEXT NOT NULL,
    run_id        INTEGER NOT NULL REFERENCES agent_runs(run_id),
    account_id    TEXT NOT NULL,
    as_of         TEXT NOT NULL,
    model_id      TEXT NOT NULL REFERENCES model_versions(model_id),
    p_lost        REAL NOT NULL,       -- P(next renewal is lost)
    arr           REAL,
    arr_at_risk   REAL NOT NULL,       -- p_lost x ARR: the ranking key
    renewal_date  TEXT NOT NULL,
    blind_spot    INTEGER NOT NULL,    -- 1 = too little data to rank with the rest
    features      TEXT NOT NULL,       -- JSON: raw input values (null = missing)
    contributions TEXT NOT NULL,       -- JSON: log-odds contribution per model input
    coverage      TEXT NOT NULL,       -- JSON: recent event counts per source
    UNIQUE (run_id, account_id)
);
CREATE INDEX IF NOT EXISTS ix_scores_rank ON scores (tenant_id, run_id, arr_at_risk);
CREATE INDEX IF NOT EXISTS ix_scores_account ON scores (tenant_id, account_id, as_of);

CREATE TABLE IF NOT EXISTS tasks (
    task_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id     TEXT NOT NULL,
    account_id    TEXT NOT NULL,
    csm_id        TEXT,
    run_id        INTEGER NOT NULL REFERENCES agent_runs(run_id),
    score_id      INTEGER NOT NULL REFERENCES scores(score_id),   -- the score that triggered it
    kind          TEXT NOT NULL,
    reason        TEXT NOT NULL,
    draft_to      TEXT,
    draft_subject TEXT,
    draft_body    TEXT,
    status        TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'sent', 'dismissed')),
    created_at    TEXT NOT NULL,
    resolved_at   TEXT,
    resolved_by   TEXT,
    UNIQUE (run_id, account_id)
);

-- Emails a CSM approved. The demo records them; nothing is delivered for real.
CREATE TABLE IF NOT EXISTS outbox (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    task_id   INTEGER NOT NULL REFERENCES tasks(task_id),
    to_email  TEXT NOT NULL,
    subject   TEXT NOT NULL,
    body      TEXT NOT NULL,
    sent_by   TEXT NOT NULL,
    sent_at   TEXT NOT NULL
);
