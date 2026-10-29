# CSM Worklist

A weekly ranked worklist for Customer Success Managers, kept current by an unattended agent.

- Solution brief: [`docs/BRIEF.md`](docs/BRIEF.md)

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

- **Live demo:** _URL added after the first deploy_
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

### Deploying your own copy

1. Push this repo to GitHub.
2. In Render: **New → Blueprint**, pick the repo. When asked for `WORKLIST_AGENT_TOKEN`,
   paste a long random string (for example the output of `python -c "import secrets; print(secrets.token_urlsafe(32))"`).
3. In GitHub, under **Settings → Secrets and variables → Actions**: add the secret
   `WORKLIST_AGENT_TOKEN` (same value) and the variable `WORKLIST_URL` (the Render URL, no trailing slash).
4. **Actions → Weekly agent run → Run workflow** to check the scheduler end to end.
