# Equipment Maintenance Triage Assistant

A web app where a technician reports an equipment problem and gets AI-assisted triage backed by the equipment manuals: possible causes, follow-up questions, inspection steps, a suggested priority and a draft work order. Every AI suggestion cites the manual section or report evidence behind it. A technician reviews, edits and approves or rejects the work order.

**The AI never controls equipment and never approves work.** Approval and rejection are technician actions only.

Live demo: https://triage-assistant-dqyq.onrender.com/ (free hosting: the first load after idle can take about a minute, and demo data resets on restart)

## What it does

1. **Report form**: equipment type and identifier, issue description, recent operating events (one per line), and optional sensor readings.
2. **Deterministic threshold checks** (no AI): limits live in `app/thresholds.json`. Results are labelled OK, WARNING or CRITICAL.
3. **Data quality handling**: missing readings, invalid or implausible values, and conflicts (the description says "overheating" but the sensor reads normal) are flagged, never silently ignored.
4. **Manual retrieval**: BM25 search over the sections of `kb/*.md`. Sections have ids such as `pump:2`, used as citations. If retrieval fails, a clear banner is shown and the threshold checks still display.
5. **AI analysis** (Gemini): returns strict JSON that is validated. Every cause, question, step and task must cite a real id (a retrieved manual section, `event:N`, `sensor:<name>` or `report:description`). Output with missing or unknown citations is rejected, retried once, then shown as a failure. Temporary errors (429, 503, timeouts) are retried automatically.
6. **Priority floor**: the AI may suggest a priority, but a critical rule result forces at least HIGH and a warning at least MEDIUM. The page shows when the rules raised it.
7. **Work orders**: the AI creates a `draft`. A technician can edit it, then approve or reject it. Approved and rejected orders are locked. Every change is recorded in a history log.
8. **Three kinds of statements are kept apart**: observations (rule results), possible causes (AI, always labelled POSSIBLE) and confirmed findings (only a technician can record these).
9. **History**: per-equipment list of reports, findings and work orders.

## Tech stack

Python, FastAPI, SQLAlchemy with SQLite, Jinja2 templates, rank-bm25, Pydantic validation, Gemini REST API, pytest.

## Project layout

```
app/
  main.py          report submission, AI analysis route, JSON API
  workflow.py      work order edit/approve/reject, confirmed findings, equipment history
  rules.py         deterministic threshold and data quality checks
  thresholds.json  limits per equipment type
  retrieval.py     manual chunking and BM25 retrieval
  ai.py            Gemini call, prompt, JSON and citation validation, retry
  models.py        database models
  templates/       HTML pages
kb/                manuals (markdown, sections split by "## n. Title")
scripts/seed.py    demo data
tests/             unit and flow tests
```

## Run locally

Requires Python 3.12 or newer.

```bash
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1      Mac/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # Windows: copy .env.example .env   then put your key in .env
python scripts/seed.py      # optional demo data
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000 (live demo: https://triage-assistant-dqyq.onrender.com/). A free Gemini API key is available from Google AI Studio.

| Variable | Purpose |
|---|---|
| `GEMINI_API_KEY` | API key for the AI step (required for AI analysis only) |
| `GEMINI_MODEL` | Model name (default `gemini-2.5-flash`) |

Without a key, everything except AI analysis works and the AI step shows a clear error.

## Demo data

`python scripts/seed.py` adds four reports: a critical pump, a motor with conflicting sensor data, a compressor with a low-pressure warning and partial readings, and a conveyor with no sensor data. Open a report and click **Run AI analysis**.

## Tests

```bash
pytest
```

Tests cover the threshold rules, missing/invalid/conflicting data, retrieval and retrieval failure, AI output validation (bad JSON, missing or unknown citations), retry behaviour, the priority floor, the approval flow and locking, confirmed findings, and equipment history. The AI is mocked in tests.

## Deployment (Render)

Build command: `pip install -r requirements.txt`
Start command: `python scripts/seed.py && uvicorn app.main:app --host 0.0.0.0 --port $PORT`
Environment: `GEMINI_API_KEY`, `GEMINI_MODEL`, `PYTHON_VERSION` (see `render.yaml`).

## Design decisions

- **Rules before AI.** Safety-relevant limits are deterministic and always shown, even if retrieval or the AI fails.
- **Citations are enforced in code**, not just requested in the prompt.
- **Report text is treated as untrusted** in the prompt (injection guard), and the AI output is never executed.
- **Retrieval is BM25 on small manual sections.** It is transparent, needs no vector database and is easy to test.
- **Audit log** records report creation, AI results and failures, edits, approvals and rejections.

## Limitations

- No user accounts: all actions are logged as "technician".
- SQLite on free hosting is not persistent across restarts.
- Warning limits come from the manuals. The **critical** limits are my own assumptions where a manual only defines a warning level.
- Report text is sent to the Gemini API for analysis.
- Not included: live IoT integration, predictive maintenance, inventory, dispatch.

## AI tool use

This project was built with AI coding assistance (Claude). I reviewed, ran and tested the code and I am responsible for all of it. The in-app AI only suggests; a technician makes every decision.
