"""
Step 3: manual retrieval (BM25) + retrieval failure handling + tests.
Run from the triage folder (venv active, server stopped):  python setup_step3.py
Overwrites app/main.py and app/templates/report.html, patches app/models.py.
"""
import os
import re

FILES = {}

FILES["app/retrieval.py"] = r'''"""Retrieve relevant manual sections (BM25 over ## sections of kb/*.md).
Chunk id format: <manual>:<section number>, e.g. pump:2. Used later as citations."""
import re
from pathlib import Path

from rank_bm25 import BM25Okapi

KB_DIR = Path(__file__).resolve().parent.parent / "kb"
HEADING = re.compile(r"^##\s*(\d+)\.\s*(.+?)\s*$", re.M)
STOP = {"the", "an", "and", "or", "of", "to", "is", "are", "in", "on", "at", "for",
        "with", "it", "this", "that", "was", "be", "by", "as", "from"}


class RetrievalError(Exception):
    """Raised when no usable manual evidence can be retrieved."""


def load_chunks(kb_dir=KB_DIR):
    kb_dir = Path(kb_dir)
    chunks = []
    if not kb_dir.is_dir():
        return chunks
    for path in sorted(kb_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        matches = list(HEADING.finditer(text))
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            chunks.append({
                "id": f"{path.stem}:{m.group(1)}",
                "manual": path.stem,
                "title": m.group(2),
                "text": text[m.end():end].strip(),
            })
    return chunks


def _tokens(s):
    return [t for t in re.findall(r"[a-z0-9]+", s.lower()) if len(t) > 1 and t not in STOP]


def build_query(description, events, observations):
    """Description + events + the text of critical/warning rule observations."""
    parts = [description or "", events or ""]
    parts += [o["text"] for o in observations if o["severity"] in ("critical", "warning")]
    return " ".join(parts)


def retrieve(equipment_type, description, events="", observations=(), k=4, kb_dir=KB_DIR):
    chunks = [c for c in load_chunks(kb_dir) if c["manual"] == equipment_type]
    if not chunks:
        raise RetrievalError(f"No manual found for equipment type '{equipment_type}'.")
    q = _tokens(build_query(description, events, observations))
    if not q:
        raise RetrievalError("The report has no searchable text.")
    bm25 = BM25Okapi([_tokens(c["title"] + " " + c["text"]) for c in chunks])
    scores = bm25.get_scores(q)
    ranked = sorted(zip(chunks, scores), key=lambda x: x[1], reverse=True)
    hits = [(c, s) for c, s in ranked if s > 0][:k]
    if not hits:
        raise RetrievalError("No relevant manual sections found for this report.")
    # always include the safety section so work orders can cite it
    if not any(c["title"].lower().startswith("safety") for c, _ in hits):
        for c, _ in ranked:
            if c["title"].lower().startswith("safety"):
                hits.append((c, 0.0))
                break
    return [{"id": c["id"], "title": c["title"], "text": c["text"], "score": round(float(s), 3)} for c, s in hits]
'''

FILES["app/templates/report.html"] = r'''{% extends "base.html" %}
{% block title %}Report #{{ report.id }}{% endblock %}
{% block content %}
<h2>Report #{{ report.id }}: {{ report.equipment.type|capitalize }} {{ report.equipment.identifier }}</h2>
<p class="muted">Submitted {{ report.created_at.strftime("%Y-%m-%d %H:%M") }} UTC</p>

<h3>Issue description</h3>
<p>{{ report.description }}</p>

<h3>Recent operating events</h3>
{% if events %}
<ol>
  {% for e in events %}<li><span class="tag">event:{{ loop.index }}</span> {{ e }}</li>{% endfor %}
</ol>
{% else %}
<p class="muted">None provided.</p>
{% endif %}

{% set flags = report.findings|selectattr("severity", "equalto", "data_quality")|list %}
{% set checks = report.findings|rejectattr("severity", "equalto", "data_quality")|list %}

<h3>Data quality flags</h3>
{% if flags %}
  {% for f in flags %}<div class="item data_quality"><span class="tag">{{ f.source }}</span> {{ f.text }}</div>{% endfor %}
{% else %}
  <p class="muted">No missing, invalid or conflicting data detected.</p>
{% endif %}

<h3>Observations: deterministic threshold checks</h3>
{% if checks %}
  {% for f in checks %}<div class="item {{ f.severity }}"><span class="tag">{{ f.source }}</span> <b>{{ f.severity|upper }}</b> {{ f.text }}</div>{% endfor %}
{% else %}
  <p class="muted">No checks ran.</p>
{% endif %}

<h3>Retrieved manual sections</h3>
{% if report.retrieval_error %}
  <div class="errors"><b>Retrieval failed:</b> {{ report.retrieval_error }}
  The threshold checks above are still valid. AI analysis needs manual evidence, so it will not run.</div>
{% elif report.retrieved %}
  {% for c in report.retrieved %}
  <div class="item info"><span class="tag">{{ c.id }}</span> <b>{{ c.title }}</b> <span class="muted">(score {{ c.score }})</span><br>{{ c.text }}</div>
  {% endfor %}
{% else %}
  <p class="muted">Nothing retrieved.</p>
{% endif %}

<h3>AI analysis</h3>
<div class="placeholder">Not run yet. (Added in the next step: possible causes, follow-up questions, inspection steps, priority and draft work order, all with citations.)</div>
{% endblock %}
'''

FILES["app/main.py"] = r'''import logging
import math
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .db import Base, engine, get_db
from .retrieval import RetrievalError, retrieve
from .rules import SENSOR_LABELS, THRESHOLDS, check_sensors

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("triage")

Base.metadata.create_all(engine)
app = FastAPI(title="Equipment Maintenance Triage Assistant")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def new_report_form(request: Request):
    return templates.TemplateResponse(
        request, "new_report.html",
        {"types": list(THRESHOLDS), "sensor_labels": SENSOR_LABELS, "values": {}, "errors": []},
    )


@app.post("/reports")
async def create_report(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    etype = str(form.get("equipment_type", "")).strip().lower()
    ident = str(form.get("identifier", "")).strip()
    desc = str(form.get("description", "")).strip()
    events = str(form.get("recent_events", "")).strip()

    errors = []
    if etype not in THRESHOLDS:
        errors.append("Choose a valid equipment type.")
    if not ident:
        errors.append("Equipment identifier is required.")
    if not desc:
        errors.append("Issue description is required.")

    eq = None
    if not errors:
        eq = db.scalar(select(models.Equipment).where(models.Equipment.identifier == ident))
        if eq and eq.type != etype:
            errors.append(f"{ident} is already registered as a {eq.type}, not a {etype}.")

    if errors:
        return templates.TemplateResponse(
            request, "new_report.html",
            {"types": list(THRESHOLDS), "sensor_labels": SENSOR_LABELS, "values": dict(form), "errors": errors},
            status_code=400,
        )

    sensors = {}
    for key in SENSOR_LABELS:
        raw = str(form.get(key, "")).strip()
        if raw == "":
            continue
        try:
            num = float(raw)
            sensors[key] = num if math.isfinite(num) else raw
        except ValueError:
            sensors[key] = raw  # kept as text so the rules can flag it as invalid

    if eq is None:
        eq = models.Equipment(type=etype, identifier=ident)
        db.add(eq)
        db.flush()

    report = models.Report(equipment_id=eq.id, description=desc, recent_events=events, sensors=sensors or None)
    db.add(report)
    db.flush()

    # 1) deterministic checks (always run, no AI)
    observations = check_sensors(etype, sensors or None, desc)
    for item in observations:
        db.add(models.Finding(
            report_id=report.id, kind="observation", origin="rule",
            text=item["text"], source=item["source"], severity=item["severity"],
        ))

    # 2) manual retrieval (failure is shown to the user, never hidden)
    try:
        report.retrieved = retrieve(etype, desc, events, observations)
        logger.info("report %s: retrieved %d manual sections", report.id, len(report.retrieved))
    except RetrievalError as exc:
        report.retrieval_error = str(exc)
        logger.warning("report %s: retrieval failed: %s", report.id, exc)
    except Exception as exc:  # unexpected failure, still shown clearly
        report.retrieval_error = f"Unexpected retrieval error: {exc}"
        logger.exception("report %s: unexpected retrieval error", report.id)

    db.add(models.AuditLog(entity="report", entity_id=report.id, action="created", actor="technician"))
    db.commit()
    return RedirectResponse(f"/reports/{report.id}", status_code=303)


@app.get("/reports/{report_id}")
def show_report(report_id: int, request: Request, db: Session = Depends(get_db)):
    report = db.get(models.Report, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    events = [e.strip() for e in (report.recent_events or "").split("\n") if e.strip()]
    return templates.TemplateResponse(request, "report.html", {"report": report, "events": events})


@app.get("/api/reports/{report_id}")
def report_json(report_id: int, db: Session = Depends(get_db)):
    report = db.get(models.Report, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return {
        "id": report.id,
        "equipment": {"type": report.equipment.type, "identifier": report.equipment.identifier},
        "description": report.description,
        "recent_events": [e.strip() for e in (report.recent_events or "").split("\n") if e.strip()],
        "sensors": report.sensors,
        "retrieved": report.retrieved,
        "retrieval_error": report.retrieval_error,
        "findings": [
            {"kind": f.kind, "origin": f.origin, "severity": f.severity, "text": f.text, "source": f.source}
            for f in report.findings
        ],
    }
'''

FILES["tests/test_retrieval.py"] = r'''import pytest

import app.main as main_mod
from app.retrieval import RetrievalError, load_chunks, retrieve
from tests.test_reports import make_client


def ids(hits):
    return [h["id"] for h in hits]


def test_chunk_ids_are_parsed_from_kb():
    found = {c["id"] for c in load_chunks()}
    assert "pump:2" in found and "motor:1" in found and "conveyor:4" in found


def test_bearing_temperature_query_finds_fault_section():
    hits = retrieve("pump", "high bearing temperature")
    assert "pump:2" in ids(hits)


def test_vibration_query_finds_vibration_section():
    hits = retrieve("pump", "strong vibration and shaking")
    assert "pump:3" in ids(hits)


def test_results_only_come_from_the_matching_manual():
    hits = retrieve("motor", "overheating winding")
    assert all(i.startswith("motor:") for i in ids(hits))
    assert "motor:2" in ids(hits)


def test_safety_section_is_always_included():
    hits = retrieve("pump", "high bearing temperature")
    assert "pump:5" in ids(hits)


def test_nonsense_query_raises_retrieval_error():
    with pytest.raises(RetrievalError):
        retrieve("pump", "zzzz qqqq")


def test_empty_kb_raises_retrieval_error(tmp_path):
    with pytest.raises(RetrievalError):
        retrieve("pump", "high temperature", kb_dir=tmp_path)


def test_report_page_shows_retrieved_sections():
    client = make_client()
    r = client.post("/reports", data={
        "equipment_type": "pump", "identifier": "P-201",
        "description": "high bearing temperature", "temp_c": "98",
    }, follow_redirects=False)
    page = client.get(r.headers["location"])
    assert "pump:2" in page.text


def test_retrieval_failure_is_shown_but_threshold_checks_still_display(monkeypatch):
    def boom(*args, **kwargs):
        raise RetrievalError("Knowledge base unavailable")

    monkeypatch.setattr(main_mod, "retrieve", boom)
    client = make_client()
    r = client.post("/reports", data={
        "equipment_type": "pump", "identifier": "P-202",
        "description": "pump running hot", "temp_c": "98",
    }, follow_redirects=False)
    page = client.get(r.headers["location"])
    assert "Retrieval failed" in page.text
    assert "Knowledge base unavailable" in page.text
    assert "CRITICAL" in page.text
'''


def patch_models():
    path = "app/models.py"
    if not os.path.exists(path):
        print("WARNING: app/models.py not found. Run from the triage folder.")
        return
    with open(path, encoding="utf-8") as f:
        s = f.read()
    if "retrieval_error" in s:
        print("models.py already patched")
        return
    add = (
        "    retrieved: Mapped[list | None] = mapped_column(JSON, nullable=True)  # manual sections used\n"
        "    retrieval_error: Mapped[str | None] = mapped_column(Text, nullable=True)\n"
        "    ai_output: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # follow-ups, inspection steps\n"
    )
    new, n = re.subn(r"(    ai_error: .*\n)", lambda m: m.group(1) + add, s, count=1)
    if n == 0:
        print("WARNING: could not patch models.py. Add these lines to class Report, after ai_error:")
        print(add)
        return
    with open(path, "w", encoding="utf-8") as f:
        f.write(new)
    print("patched: app/models.py (added Report.retrieved, retrieval_error, ai_output)")


def main():
    for path, content in FILES.items():
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"written: {path}")
    patch_models()
    try:
        if os.path.exists("triage.db"):
            os.remove("triage.db")
            print("removed old triage.db (will be recreated on start)")
    except OSError:
        print("Could not delete triage.db. Stop the server (Ctrl+C) and delete triage.db manually, then rerun.")
    print("\nDone. Next:")
    print("  pytest")
    print("  uvicorn app.main:app --reload")


if __name__ == "__main__":
    main()
