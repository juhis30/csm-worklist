# CSM Worklist

A weekly ranked worklist for Customer Success Managers, kept current by an unattended agent.

- Solution brief: [`docs/BRIEF.md`](docs/BRIEF.md)
- Edge cases, trade-offs and details: [`docs/DECISIONS.md`](docs/DECISIONS.md)
- The synthetic data, in plain terms: [`docs/DATA.md`](docs/DATA.md)
- Slides: [`docs/CSM-Worklist-brief.pptx`](docs/CSM-Worklist-brief.pptx)

## What I decided

The full reasoning is in [`docs/DECISIONS.md`](docs/DECISIONS.md). The short version:

- **A ranked worklist, not a red/yellow/green dashboard.** Accounts are ordered by expected ARR at risk
  (P(renewal lost) × ARR), with the reasons shown. Accounts we know too little about go on a separate
  blind-spots list instead of looking healthy.
- **The model predicts the next renewal being lost**, the outcome the business cares about, using real
  labels from Salesforce. Logistic regression, one per organisation, so every score is an exact,
  explainable sum. Tested on 6 months it never saw, against a sensible spreadsheet rule.
- **The agent drafts, a person sends.** A weekly unattended run opens tasks only when something changed,
  each with a drafted email that a CSM approves or dismisses. Nothing reaches a customer automatically.
- **Every number traces back** to the model version, its inputs and the raw events behind them.
- **Organisations are isolated** in code (every query is forced through a tenant filter) and in modelling
  (no organisation's data trains another's model).

## What I deliberately skipped

| Skipped | Why | What it would take |
|---|---|---|
| Real integrations (Salesforce, Zendesk, Stripe, Gmail APIs) | The exercise asks for generated data. The hard part, messy data, is in the generator and the ingestion code. | A connector per source writing into `raw_events`; ingestion already handles late, duplicate and unmatched records. |
| Real login | A persona picker makes the demo easy to explore. | SSO per customer organisation, mapped to the existing users and roles. |
| Actually sending email | Approved drafts go to an `outbox` table. Sending needs email policy and consent answers from the business. | A mail provider behind the outbox, with an audit trail. |
| LLM-written drafts | Templates guarantee every sentence matches a computed fact. | An LLM could rephrase a template, with checks that it adds no new claims. |
| Account history (ARR, owner over time) | A small, documented leak: past snapshots use today's CRM values. | A history table and as-of joins. |
| A cap on the first week's tasks | The first run opens ~10–13 tasks per CSM; afterwards it settles to ~1 per week. It only happens once. | Cap each CSM's first run to their top 5. |
| Persistent storage | Free hosting: every restart returns to the same demo state. | A paid instance with a disk, or Postgres. |
| Seat adoption (active users ÷ seats) | The synthetic data doesn't vary it independently of usage, so it adds nothing here. | Worth testing first on real data. |
| Measuring whether it works in practice | Needs real CSMs and real renewals. | Compare renewal rates for accounts that got a task vs similar ones that didn't. |

## What breaks first at 10x

10x means about 50,000 accounts and over 2 million events. Measured on a laptop, memory for a
run that retrains grows in a straight line with data size:

| World size | Peak memory | Run time |
|---|---|---|
| Code loaded, no data | 151 MB | – |
| 30% | 192 MB | 8 s |
| 100% (today) | 293 MB | 19 s |
| **10x (projected)** | **about 1.6 GB** | **about 3 min** (longer on a shared server) |

What fails, in order:

1. **Memory: the weekly run crashes, and takes the website with it.** Each run loads every event the
   organisation has ever sent into memory to compute features and retrain. On the 512 MB instance this
   runs out at roughly **2.5x** today's data, before 10x. Because the agent runs inside the web process,
   a crash also takes the site down.
   *Fix:* run the agent as its own job, and keep a weekly per-account summary table so each run reads
   only new events instead of all of history.
2. **CSMs' clicks fail during a run.** A run is one database transaction, which keeps it all-or-nothing,
   but SQLite allows one writer at a time. At 10x the run holds that lock for minutes, and a CSM
   approving a task waits up to 30 seconds and then gets an error.
   *Fix:* do the computation outside the transaction and only write the results in it, or move to Postgres.
3. **The Data page times out.** It already takes about 11 s for the largest organisation on the free
   instance, and that grows with data size.
   *Fix:* the run saves the stats, and the page reads them.
4. **The scheduler call times out.** GitHub Actions waits up to 15 minutes for the run to finish.
   *Fix:* the endpoint starts the run and returns at once; the workflow checks the result afterwards.

What holds up: the model itself (more data helps it), the ranking, and the tenant isolation.
More organisations, as opposed to bigger ones, mostly cost time, because each one runs separately.

## Stack

Python 3.12 · FastAPI · SQLite · server-rendered Jinja templates · pandas · scikit-learn.
One language, no frontend build step, one process.

## Layout

```
app/          application code
  templates/  HTML pages
tests/        pytest suite
docs/         solution brief
data/         generated SQLite database (not committed)
```

## Running locally

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows; use source .venv/bin/activate elsewhere
pip install -r requirements.txt
pytest -q

python -m app.bootstrap                    # build the demo database (about 1-2 minutes)
uvicorn app.web:app --reload               # http://127.0.0.1:8000
python -m app.agent                        # run the weekly agent once by hand
```

## Deployment

- **Live demo:** https://csm-worklist.onrender.com (pick any persona on the login page;
  the first visit after an idle spell takes about a minute while the free instance wakes)
- **Hosting:** one free Render web service, defined in [`render.yaml`](render.yaml).
- **Scheduler:** GitHub Actions ([`agent.yml`](.github/workflows/agent.yml)) calls
  `POST /internal/agent/run` every Monday at 06:00 UTC with a shared bearer token.
  The "Run workflow" button triggers a run on demand.
- **CI:** [`tests.yml`](.github/workflows/tests.yml) runs the test suite on every push.

**Demo trade-off:** the free tier has no persistent disk and sleeps after 15 idle minutes.
The database is built during the deploy, one simulated week behind, so every wake-up
starts from the same state and the next agent run has a real week of new data to process.
Task clicks and run results last until the service sleeps. In production this would be a
paid instance with a persistent disk (or Postgres), with `WORKLIST_CLOCK=wall`.

Measured memory peaks: 381 MB to build the database, 289 MB for a run that retrains
all three organisations. Both are under the free tier's 512 MB.

**Known limitation:** the Data page recounts every raw event on each visit. For Northwind
(~140k events) that takes about 11 s on the free instance (2.5 s on a laptop). The counts
only change when the agent runs, so the fix is for each run to save them and the page to read them.

### Deploying your own copy

1. Push this repo to GitHub.
2. In Render: **New → Blueprint**, pick the repo. When asked for `WORKLIST_AGENT_TOKEN`,
   paste a long random string (for example the output of `python -c "import secrets; print(secrets.token_urlsafe(32))"`).
3. In GitHub, under **Settings → Secrets and variables → Actions**: add the secret
   `WORKLIST_AGENT_TOKEN` (same value) and the variable `WORKLIST_URL` (the Render URL, no trailing slash).
4. **Actions → Weekly agent run → Run workflow** to check the scheduler end to end.
