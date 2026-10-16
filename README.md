# Equipment Maintenance Triage Assistant

A web app where a technician reports an equipment problem and gets AI-assisted triage backed by the equipment manuals: possible causes, follow-up questions, inspection steps, a suggested priority and a draft work order. Every AI suggestion cites the manual section or report evidence behind it. A technician reviews, edits and approves or rejects the work order.

**The AI never controls equipment and never approves work.** Approval and rejection are technician actions only.

- **Live demo:** https://triage-assistant-dqyq.onrender.com/
- **Agent usage notes:** [AGENT_USAGE.md](AGENT_USAGE.md)

The demo runs on free hosting: the first load after idle can take about a minute, and data resets when the service restarts (demo reports are re-seeded automatically). No login is needed.

## Reviewer quick start

1. Open the live demo and click **History**. Four demo reports are loaded (P-101, M-204, C-310, CV-12).
2. Open a report and click **Run AI analysis** (5 to 60 seconds). You get possible causes with citations, follow-up questions, inspection steps, a priority and a draft work order.
3. Click **Edit / approve / reject** on the work order, edit it, then approve or reject. Approved and rejected orders are locked, and every change appears in the work order history.
4. Record a **confirmed finding** on the report page. Only a technician can do this.

Sample inputs to try on **New report**:

| Case | Input | What to expect |
|---|---|---|
| Critical threshold | Pump `P-500`, "high bearing temperature", event "Restarted after maintenance yesterday", temperature `98` | CRITICAL result, missing-reading flags, AI priority raised by rules |
| Conflicting data | Motor `M-500`, "motor is overheating", temperature `60` | Conflict flag: description says overheating, sensor reads normal |
| Invalid reading | Pump `P-501`, "pump running hot", temperature `abc` | Reading flagged as invalid and not used |
| Retrieval failure | Pump `P-502`, description `zzzz qqqq`, no sensors | Red "Retrieval failed" banner, AI analysis blocked |

## Features

1. **Report form**: equipment type and identifier, issue description, recent operating events (one per line), optional sensor readings.
2. **Deterministic threshold checks** (no AI): limits live in `app/thresholds.json`. Results are OK, WARNING or CRITICAL.
3. **Data quality handling**: missing readings, invalid or implausible values and conflicts between description and sensors are flagged, never silently ignored.
4. **Manual retrieval**: BM25 over the sections of `kb/*.md`. Sections have ids such as `pump:2`, used as citations. If retrieval fails, a clear banner shows and the threshold checks still display.
5. **AI analysis** (Gemini): strict JSON, validated. Every cause, question, step and task must cite a real id (a retrieved manual section, `event:N`, `sensor:<name>` or `report:description`). Output with missing or unknown citations is rejected, retried once, then shown as a failure. Temporary errors (429, 503, timeouts) are retried automatically.
6. **Priority floor**: a critical rule result forces at least HIGH and a warning at least MEDIUM. The page shows when the rules raised the AI's suggestion.
7. **Work orders**: the AI creates a `draft`. A technician edits it and approves or rejects it. Decisions lock the order. All changes go to an audit history.
8. **Three kinds of statements stay separate**: observations (rule results), possible causes (AI, labelled POSSIBLE) and confirmed findings (technician only).
9. **History**: per-equipment list of reports, findings and work orders.
10. **UI states**: loading (buttons disable and show progress), empty, validation, success notices, failure banners and friendly error pages.

## Architecture

```
Technician (browser)
   |
   v
FastAPI + Jinja pages
   |-- POST /reports ------------> rules.py (thresholds.json)  -> findings (observation)
   |                          \-> retrieval.py (BM25 over kb/*.md) -> report.retrieved / retrieval_error
   |-- POST /reports/{id}/analyze -> ai.py -> Gemini API
   |                                   -> validate JSON + citations (retry once)
   |                                   -> possible causes, steps, priority, draft work order
   |-- /work-orders/{id} ---------> technician edit / approve / reject -> audit_log
   |-- /equipment ----------------> history
   v
SQLite: equipment, reports, findings, work_orders, audit_log
Logs: JSON lines on stdout
```

## Scope

**Completed**
- Report intake with structured sensor readings, deterministic checks, retrieval, AI analysis with citations, draft work order, technician edit/approve/reject, confirmed findings, per-equipment history, audit trail, structured logs, tests, deployment.

**Intentionally excluded**
- Live IoT integration, predictive maintenance models, inventory management and technician dispatch.
- User accounts and roles: all actions are logged as "technician".
- Remote control of equipment (never) and automatic approval (never).
- Persistent production database: SQLite is used for simplicity.

## Tech stack

Python, FastAPI, SQLAlchemy with SQLite, Jinja2 templates, rank-bm25, Pydantic validation, Gemini REST API, pytest.

## Project layout

```
app/
  main.py          report submission, AI analysis route, JSON API, error pages
  workflow.py      work order edit/approve/reject, confirmed findings, equipment history
  rules.py         deterministic threshold and data quality checks
  thresholds.json  limits per equipment type
  retrieval.py     manual chunking and BM25 retrieval
  ai.py            Gemini call, prompt, JSON and citation validation, retry
  logging_setup.py structured JSON logging
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
| `GEMINI_MODEL` | Model name (default `gemini-3.5-flash`) |

Without a key, everything except AI analysis works and the AI step shows a clear error.

## Demo data

`python scripts/seed.py` adds four reports: a critical pump, a motor with conflicting sensor data, a compressor with a low-pressure warning and partial readings, and a conveyor with no sensor data. It skips if data already exists (`--force` adds them again).

## Logging

Logs are JSON lines on stdout (visible in the Render Logs tab). Each line has `ts`, `level`, `logger`, `event` and event fields. Events include:

- `report_created`, `report_rejected`, `retrieval_ok`, `retrieval_failed`
- `ai_analysis_started`, `ai_http_attempt` (status, latency), `ai_http_retry`, `ai_output_rejected`, `ai_output_accepted`, `ai_analysis_ok`, `ai_analysis_failed`, `ai_analysis_blocked`
- `work_order_edited`, `work_order_approved`, `work_order_rejected`, `confirmed_finding_recorded`

API keys and report text are never logged.

## Tests

```bash
pytest
```

Tests cover the threshold rules, missing/invalid/conflicting data, retrieval and retrieval failure, AI output validation (bad JSON, missing or unknown citations), retry behaviour, the priority floor, the approval flow and locking, confirmed findings, equipment history, structured logging, UI states and error pages. The AI is mocked in tests, so they need no key.

## Deployment (Render)

- Build command: `pip install -r requirements.txt`
- Start command: `python scripts/seed.py && uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Environment: `GEMINI_API_KEY`, `GEMINI_MODEL`, `PYTHON_VERSION` (see `render.yaml`). The key is set only in Render's dashboard, never in the repository.

## Design decisions

- **Rules before AI.** Safety-relevant limits are deterministic and always shown, even if retrieval or the AI fails.
- **Citations are enforced in code**, not just requested in the prompt.
- **Report text is treated as untrusted** in the prompt (injection guard), and AI output is never executed.
- **BM25 on small manual sections.** Transparent, no vector database, easy to test.
- **Audit log** records report creation, AI results and failures, edits, approvals and rejections.

## Limitations

- No user accounts: all actions are attributed to "technician".
- SQLite on free hosting is not persistent across restarts.
- Warning limits come from the manuals. The **critical** limits are my own assumptions where a manual only defines a warning level.
- Report text is sent to the Gemini API for analysis. The free tier has rate limits, so analysis can occasionally fail with a clear message and can be retried.
- Retrieval is keyword based (BM25), so unusual wording may miss a relevant section.

## AI tool use

Built with AI coding assistance. See [AGENT_USAGE.md](AGENT_USAGE.md) for tools, prompts, mistakes and verification.
