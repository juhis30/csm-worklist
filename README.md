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
```
