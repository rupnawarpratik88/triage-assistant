"""
Step 6: demo seed data, README, Render config, seed test.
Run from the triage folder (venv active, server stopped):  python setup_step6.py
Only adds new files (README.md is replaced). No database reset needed.
"""
import os

FILES = {}

FILES["scripts/__init__.py"] = ""

FILES["scripts/seed.py"] = r'''"""Load demo reports so a reviewer can try the app immediately.
Safe to run repeatedly: it skips when data already exists.
Usage:  python scripts/seed.py            (skip if data exists)
        python scripts/seed.py --force    (add the demo reports again)
The demo reports go through the real submission route, so rules and retrieval run exactly as for users.
AI analysis is not pre-filled: click "Run AI analysis" on a report."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEMO_REPORTS = [
    {   # critical threshold breach + a warning
        "equipment_type": "pump", "identifier": "P-101",
        "description": "Pump running hot and noisy after restart",
        "recent_events": "Restarted after maintenance yesterday\nLoad increased to 90% this morning",
        "temp_c": "98", "vibration_mm_s": "5.2", "pressure_bar": "4.8",
    },
    {   # conflicting data: description says overheating, sensor reads normal; vibration missing
        "equipment_type": "motor", "identifier": "M-204",
        "description": "Motor is overheating and trips occasionally",
        "recent_events": "Tripped twice on overcurrent last night",
        "temp_c": "60", "current_dev_pct": "12",
    },
    {   # low pressure warning, partial readings
        "equipment_type": "compressor", "identifier": "C-310",
        "description": "Pressure not reaching setpoint",
        "recent_events": "Intake filter last replaced 4 months ago",
        "pressure_bar": "6.2", "oil_pressure_bar": "3.1", "temp_c": "90",
    },
    {   # no sensor readings at all
        "equipment_type": "conveyor", "identifier": "CV-12",
        "description": "Belt slipping under load",
        "recent_events": "Belt tension adjusted last week",
    },
]


def seed(client):
    """Post every demo report through the app. Returns how many were created."""
    for data in DEMO_REPORTS:
        response = client.post("/reports", data=data, follow_redirects=False)
        if response.status_code != 303:
            raise RuntimeError(f"Could not create demo report for {data['identifier']}: {response.status_code}")
    return len(DEMO_REPORTS)


def main():
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select

    from app.db import SessionLocal
    from app.main import app
    from app.models import Equipment

    with SessionLocal() as db:
        existing = db.scalar(select(func.count()).select_from(Equipment))
    if existing and "--force" not in sys.argv:
        print(f"Seed skipped: {existing} equipment record(s) already exist. Use --force to add demo data anyway.")
        return
    count = seed(TestClient(app))
    print(f"Seeded {count} demo reports.")


if __name__ == "__main__":
    main()
'''

FILES["tests/test_seed.py"] = r'''from scripts.seed import DEMO_REPORTS, seed
from tests.test_reports import make_client


def test_seed_creates_all_demo_equipment():
    client = make_client()
    assert seed(client) == len(DEMO_REPORTS)
    page = client.get("/equipment").text
    for data in DEMO_REPORTS:
        assert data["identifier"] in page


def test_demo_data_covers_conflict_missing_and_critical_cases():
    client = make_client()
    seed(client)
    texts = []
    for i in range(1, len(DEMO_REPORTS) + 1):
        texts += [f["text"] for f in client.get(f"/api/reports/{i}").json()["findings"]]
    joined = " ".join(texts)
    assert "Conflict" in joined
    assert "reading is missing" in joined
    assert "critical limit" in joined
    assert "No sensor readings provided" in joined
'''

FILES["render.yaml"] = r'''services:
  - type: web
    name: triage-assistant
    runtime: python
    plan: free
    buildCommand: pip install -r requirements.txt
    startCommand: python scripts/seed.py && uvicorn app.main:app --host 0.0.0.0 --port $PORT
    healthCheckPath: /health
    envVars:
      - key: PYTHON_VERSION
        value: 3.12.8
      - key: GEMINI_MODEL
        value: gemini-3.5-flash
      - key: GEMINI_API_KEY
        sync: false
'''

FILES["README.md"] = r'''# Equipment Maintenance Triage Assistant

A web app where a technician reports an equipment problem and gets AI-assisted triage backed by the equipment manuals: possible causes, follow-up questions, inspection steps, a suggested priority and a draft work order. Every AI suggestion cites the manual section or report evidence behind it. A technician reviews, edits and approves or rejects the work order.

**The AI never controls equipment and never approves work.** Approval and rejection are technician actions only.

Live demo: YOUR-LIVE-URL (free hosting: the first load after idle can take about a minute, and demo data resets on restart)

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

Open http://127.0.0.1:8000. A free Gemini API key is available from Google AI Studio.

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
'''


def main():
    for path, content in FILES.items():
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"written: {path}")
    print("\nDone. Next:")
    print("  pytest")
    print("  python scripts/seed.py --force")
    print("  uvicorn app.main:app --reload")


if __name__ == "__main__":
    main()
