"""
Step 7: submission requirements.
 - structured JSON logs for application and AI workflow events
 - loading / success / failure / error-page states in the UI
 - README rewrite (architecture, completed and excluded scope, reviewer quick start)
 - AGENT_USAGE.md
 - tests for logging and UI states
Run from the triage folder (venv active, server stopped):  python setup_step7.py
Overwrites app/main.py, app/workflow.py, app/templates/base.html, README.md; patches app/ai.py and report.html.
No database reset needed.
"""
import os

FILES = {}

FILES["app/logging_setup.py"] = r'''"""Structured (JSON lines) logging for application and AI-workflow events."""
import json
import logging
import sys
from datetime import datetime, timezone

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record):
        data = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED:
                data[key] = value
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str)


def setup_logging(level=logging.INFO):
    root = logging.getLogger()
    if getattr(root, "_triage_configured", False):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.handlers[:] = [handler]
    root.setLevel(level)
    root._triage_configured = True


def log_event(logger, event, level=logging.INFO, **fields):
    """Log one named event with structured fields (report_id, latency_ms, ...)."""
    logger.log(level, event, extra={"event": event, **fields})
'''

FILES["app/main.py"] = r'''import logging
import math
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import models
from .ai import AIError, analyze
from .db import Base, engine, get_db
from .logging_setup import log_event, setup_logging
from .retrieval import RetrievalError, retrieve
from .rules import SENSOR_LABELS, THRESHOLDS, check_sensors
from .workflow import router as workflow_router

setup_logging()
logger = logging.getLogger("triage")

Base.metadata.create_all(engine)
app = FastAPI(title="Equipment Maintenance Triage Assistant")
app.include_router(workflow_router)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

ERROR_TITLES = {400: "Bad request", 404: "Not found", 409: "Action not allowed"}


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return templates.TemplateResponse(
        request, "error.html",
        {"status_code": exc.status_code, "title": ERROR_TITLES.get(exc.status_code, "Error"), "detail": exc.detail},
        status_code=exc.status_code,
    )


@app.exception_handler(Exception)
async def server_error(request: Request, exc: Exception):
    logger.exception("unhandled_error", extra={"event": "unhandled_error", "path": request.url.path})
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Internal server error"}, status_code=500)
    return templates.TemplateResponse(
        request, "error.html",
        {"status_code": 500, "title": "Server error", "detail": "Something went wrong. The error was logged."},
        status_code=500,
    )


def _ms(started):
    return int((time.monotonic() - started) * 1000)


def _events(report):
    return [e.strip() for e in (report.recent_events or "").split("\n") if e.strip()]


def _redirect(report_id, notice=None):
    url = f"/reports/{report_id}"
    if notice:
        url += f"?notice={notice}"
    return RedirectResponse(url, status_code=303)


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
        log_event(logger, "report_rejected", logging.INFO, errors=errors)
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
    counts = {s: sum(1 for o in observations if o["severity"] == s) for s in ("critical", "warning", "data_quality")}

    # 2) manual retrieval (failure is shown to the user, never hidden)
    started = time.monotonic()
    try:
        report.retrieved = retrieve(etype, desc, events, observations)
        log_event(logger, "retrieval_ok", report_id=report.id, sections=len(report.retrieved),
                  top_section=report.retrieved[0]["id"], latency_ms=_ms(started))
    except RetrievalError as exc:
        report.retrieval_error = str(exc)
        log_event(logger, "retrieval_failed", logging.WARNING, report_id=report.id, error=str(exc))
    except Exception as exc:  # unexpected failure, still shown clearly
        report.retrieval_error = f"Unexpected retrieval error: {exc}"
        logger.exception("retrieval_error", extra={"event": "retrieval_error", "report_id": report.id})

    db.add(models.AuditLog(entity="report", entity_id=report.id, action="created", actor="technician"))
    db.commit()
    log_event(logger, "report_created", report_id=report.id, equipment_type=etype, identifier=ident,
              sensors_provided=len(sensors), critical=counts["critical"], warning=counts["warning"],
              data_quality=counts["data_quality"], retrieval_ok=report.retrieval_error is None)
    return _redirect(report.id, "report_created")


@app.post("/reports/{report_id}/analyze")
def analyze_report(report_id: int, db: Session = Depends(get_db)):
    report = db.get(models.Report, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    if report.ai_status == "ok":
        return _redirect(report_id)

    started = time.monotonic()

    def fail(message, event="ai_analysis_failed"):
        report.ai_status = "failed"
        report.ai_error = message
        db.add(models.AuditLog(entity="report", entity_id=report.id, action="ai_analysis_failed",
                               actor="ai", detail={"error": message}))
        db.commit()
        log_event(logger, event, logging.WARNING, report_id=report.id, error=message, latency_ms=_ms(started))
        return _redirect(report_id)

    if report.retrieval_error or not report.retrieved:
        return fail("AI analysis needs manual evidence, but retrieval failed for this report.",
                    "ai_analysis_blocked")

    log_event(logger, "ai_analysis_started", report_id=report.id, equipment_type=report.equipment.type,
              sections=len(report.retrieved))
    observations = [{"severity": f.severity, "text": f.text, "source": f.source}
                    for f in report.findings if f.origin == "rule"]
    try:
        result = analyze(report.equipment.type, report.equipment.identifier, report.description,
                         _events(report), observations, report.retrieved)
    except AIError as exc:
        return fail(str(exc))
    except Exception as exc:
        logger.exception("ai_unexpected_error", extra={"event": "ai_unexpected_error", "report_id": report.id})
        return fail(f"Unexpected AI error: {exc.__class__.__name__}")

    analysis = result["analysis"]
    for cause in analysis.possible_causes:
        db.add(models.Finding(
            report_id=report.id, kind="possible_cause", origin="ai",
            text=cause.text, source=",".join(cause.citations), severity=None,
        ))
    report.ai_output = {
        "model": result["model"],
        "follow_up_questions": [q.model_dump() for q in analysis.follow_up_questions],
        "inspection_steps": [s.model_dump() for s in analysis.inspection_steps],
        "priority": result["priority"],
    }
    report.priority = result["priority"]["level"]
    report.ai_status = "ok"
    report.ai_error = None
    order = models.WorkOrder(
        report_id=report.id, title=result["work_order_title"], body=result["work_order_body"],
        priority=result["priority"]["level"], status="draft",
    )
    db.add(order)
    db.flush()
    db.add(models.AuditLog(entity="report", entity_id=report.id, action="ai_analysis_ok", actor="ai",
                           detail={"model": result["model"]}))
    db.add(models.AuditLog(entity="work_order", entity_id=order.id, action="draft_created", actor="ai"))
    db.commit()
    log_event(logger, "ai_analysis_ok", report_id=report.id, work_order_id=order.id, model=result["model"],
              priority=result["priority"]["level"], ai_priority=result["priority"]["ai_level"],
              raised_by_rules=result["priority"]["raised_by_rules"], latency_ms=_ms(started))
    return _redirect(report_id, "analysis_ok")


@app.get("/reports/{report_id}")
def show_report(report_id: int, request: Request, db: Session = Depends(get_db)):
    report = db.get(models.Report, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return templates.TemplateResponse(request, "report.html", {"report": report, "events": _events(report)})


@app.get("/api/reports/{report_id}")
def report_json(report_id: int, db: Session = Depends(get_db)):
    report = db.get(models.Report, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return {
        "id": report.id,
        "equipment": {"type": report.equipment.type, "identifier": report.equipment.identifier},
        "description": report.description,
        "recent_events": _events(report),
        "sensors": report.sensors,
        "retrieved": report.retrieved,
        "retrieval_error": report.retrieval_error,
        "ai_status": report.ai_status,
        "ai_error": report.ai_error,
        "priority": report.priority,
        "ai_output": report.ai_output,
        "findings": [
            {"kind": f.kind, "origin": f.origin, "severity": f.severity, "text": f.text, "source": f.source}
            for f in report.findings
        ],
        "work_orders": [
            {"id": w.id, "title": w.title, "status": w.status, "priority": w.priority, "body": w.body}
            for w in report.work_orders
        ],
    }
'''

FILES["app/workflow.py"] = r'''"""Technician workflow: work order edit/approve/reject, confirmed findings, equipment history.
Only these routes can approve or reject work or create confirmed findings. The AI code never calls them."""
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .db import get_db
from .logging_setup import log_event

logger = logging.getLogger("triage.workflow")
router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
PRIORITIES = ["low", "medium", "high", "critical"]


def _get_work_order(db, wo_id):
    wo = db.get(models.WorkOrder, wo_id)
    if wo is None:
        raise HTTPException(status_code=404, detail="Work order not found")
    return wo


def _audit(db, entity, entity_id, action, detail=None, actor="technician"):
    db.add(models.AuditLog(entity=entity, entity_id=entity_id, action=action, actor=actor, detail=detail))


def _page(request, db, wo, errors=None, status_code=200):
    audit = db.scalars(
        select(models.AuditLog)
        .where(models.AuditLog.entity == "work_order", models.AuditLog.entity_id == wo.id)
        .order_by(models.AuditLog.id)
    ).all()
    return templates.TemplateResponse(
        request, "work_order.html",
        {"wo": wo, "report": wo.report, "audit": audit, "priorities": PRIORITIES, "errors": errors or []},
        status_code=status_code,
    )


def _redirect(wo_id, notice):
    return RedirectResponse(f"/work-orders/{wo_id}?notice={notice}", status_code=303)


@router.get("/work-orders/{wo_id}")
def view_work_order(wo_id: int, request: Request, db: Session = Depends(get_db)):
    return _page(request, db, _get_work_order(db, wo_id))


@router.post("/work-orders/{wo_id}/edit")
async def edit_work_order(wo_id: int, request: Request, db: Session = Depends(get_db)):
    wo = _get_work_order(db, wo_id)
    if wo.status != "draft":
        raise HTTPException(status_code=409, detail=f"Work order is {wo.status} and can no longer be edited.")
    form = await request.form()
    title = str(form.get("title", "")).strip()
    body = str(form.get("body", "")).replace("\r\n", "\n").strip()
    priority = str(form.get("priority", "")).strip().lower()
    errors = []
    if not title:
        errors.append("Title is required.")
    if not body:
        errors.append("Details are required.")
    if priority not in PRIORITIES:
        errors.append("Choose a valid priority.")
    if errors:
        log_event(logger, "work_order_edit_rejected", work_order_id=wo.id, errors=errors)
        return _page(request, db, wo, errors, 400)
    changes = {}
    for field, new in (("title", title), ("body", body), ("priority", priority)):
        old = getattr(wo, field)
        if old != new:
            changes[field] = {"before": old, "after": new}
            setattr(wo, field, new)
    if changes:
        _audit(db, "work_order", wo.id, "edited", {"changes": changes})
    db.commit()
    log_event(logger, "work_order_edited", work_order_id=wo.id, report_id=wo.report_id, fields=sorted(changes))
    return _redirect(wo.id, "wo_saved")


async def _decide(wo_id, request, db, new_status):
    wo = _get_work_order(db, wo_id)
    if wo.status != "draft":
        raise HTTPException(status_code=409, detail=f"Work order is already {wo.status}.")
    form = await request.form()
    note = str(form.get("note", "")).strip()
    wo.status = new_status
    _audit(db, "work_order", wo.id, new_status, {"note": note} if note else None)
    db.commit()
    log_event(logger, f"work_order_{new_status}", work_order_id=wo.id, report_id=wo.report_id,
              has_note=bool(note), actor="technician")
    return _redirect(wo.id, f"wo_{new_status}")


@router.post("/work-orders/{wo_id}/approve")
async def approve_work_order(wo_id: int, request: Request, db: Session = Depends(get_db)):
    return await _decide(wo_id, request, db, "approved")


@router.post("/work-orders/{wo_id}/reject")
async def reject_work_order(wo_id: int, request: Request, db: Session = Depends(get_db)):
    return await _decide(wo_id, request, db, "rejected")


@router.post("/reports/{report_id}/findings")
async def add_confirmed_finding(report_id: int, request: Request, db: Session = Depends(get_db)):
    report = db.get(models.Report, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    form = await request.form()
    text = str(form.get("text", "")).strip()
    if len(text) < 3:
        raise HTTPException(status_code=400, detail="Finding text is required (at least 3 characters).")
    finding = models.Finding(report_id=report.id, kind="confirmed_finding", origin="technician",
                             text=text, source="technician", severity=None)
    db.add(finding)
    db.flush()
    _audit(db, "finding", finding.id, "confirmed_finding_recorded", {"report_id": report.id})
    db.commit()
    log_event(logger, "confirmed_finding_recorded", report_id=report.id, finding_id=finding.id, actor="technician")
    return RedirectResponse(f"/reports/{report.id}?notice=finding_recorded", status_code=303)


@router.get("/equipment")
def equipment_list(request: Request, db: Session = Depends(get_db)):
    items = db.scalars(select(models.Equipment).order_by(models.Equipment.identifier)).all()
    return templates.TemplateResponse(request, "equipment_list.html", {"items": items})


@router.get("/equipment/{identifier}")
def equipment_detail(identifier: str, request: Request, db: Session = Depends(get_db)):
    eq = db.scalar(select(models.Equipment).where(models.Equipment.identifier == identifier))
    if eq is None:
        raise HTTPException(status_code=404, detail="Equipment not found")
    reports = sorted(eq.reports, key=lambda r: r.created_at, reverse=True)
    return templates.TemplateResponse(request, "equipment.html", {"eq": eq, "reports": reports})
'''

FILES["app/templates/base.html"] = r'''<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{% block title %}Triage Assistant{% endblock %}</title>
<style>
  body { font-family: Arial, sans-serif; margin: 0; background: #f5f6f8; color: #222; }
  header { background: #1f3a5f; color: #fff; padding: 12px 24px; display: flex; gap: 24px; align-items: center; flex-wrap: wrap; }
  header a { color: #cfe3ff; text-decoration: none; }
  .notice { background: #fff4d6; border-bottom: 1px solid #e6cf8a; padding: 8px 24px; font-size: 13px; }
  main { max-width: 860px; margin: 24px auto; background: #fff; padding: 24px; border-radius: 8px; }
  label { display: block; margin-top: 12px; font-weight: bold; font-size: 14px; }
  input, select, textarea { width: 100%; padding: 8px; margin-top: 4px; box-sizing: border-box; }
  button { margin-top: 16px; padding: 10px 18px; background: #1f3a5f; color: #fff; border: 0; border-radius: 4px; cursor: pointer; }
  button:disabled { background: #8a97a8; cursor: wait; }
  button.danger { background: #b23a3a; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0 16px; }
  .muted { color: #777; }
  .errors { background: #fde2e2; border: 1px solid #e29a9a; padding: 8px 16px; border-radius: 4px; }
  .success { background: #e3f4e6; border: 1px solid #8cc99a; padding: 8px 16px; border-radius: 4px; margin-bottom: 12px; }
  .item { padding: 8px 12px; margin: 6px 0; border-left: 5px solid #999; background: #fafafa; }
  .item.critical { border-color: #c62828; background: #fdecec; }
  .item.warning { border-color: #ef9a00; background: #fff6e0; }
  .item.ok { border-color: #2e7d32; background: #edf7ee; }
  .item.data_quality { border-color: #6a1b9a; background: #f5ecf9; }
  .item.info { border-color: #607d8b; background: #eef2f4; }
  .tag { font-family: monospace; font-size: 12px; background: #e8e8e8; padding: 1px 6px; border-radius: 3px; }
  .badge { font-size: 11px; font-weight: bold; padding: 1px 6px; border-radius: 3px; background: #e3e8f0; }
  .badge.possible { background: #fff0c2; }
  .placeholder { border: 1px dashed #aaa; padding: 12px; color: #666; border-radius: 4px; }
  pre.wo { white-space: pre-wrap; background: #fafafa; border: 1px solid #ddd; padding: 12px; border-radius: 4px; font-family: inherit; }
  @media (max-width: 640px) { .grid { grid-template-columns: 1fr; } main { margin: 12px; padding: 16px; } }
</style>
</head>
<body>
<header>
  <b>Equipment Maintenance Triage Assistant</b>
  <a href="/">New report</a>
  <a href="/equipment">History</a>
</header>
<div class="notice">Decision support only. This system never controls equipment and never approves maintenance work automatically. A technician must review and approve.</div>
<main>
{% set notices = {
  "report_created": "Report submitted. Deterministic checks and manual retrieval are done.",
  "analysis_ok": "AI analysis complete. These are suggestions, not confirmed findings. Review them below.",
  "finding_recorded": "Confirmed finding recorded.",
  "wo_saved": "Work order changes saved.",
  "wo_approved": "Work order approved by technician.",
  "wo_rejected": "Work order rejected by technician."
} %}
{% set n = request.query_params.get("notice") %}
{% if n and n in notices %}<div class="success">{{ notices[n] }}</div>{% endif %}
{% block content %}{% endblock %}
</main>
<script>
  document.addEventListener("submit", function (e) {
    var btn = e.target.querySelector("button[type=submit]");
    if (btn) {
      btn.disabled = true;
      btn.textContent = e.target.getAttribute("data-loading") || "Working...";
    }
  });
  window.addEventListener("pageshow", function (e) { if (e.persisted) { location.reload(); } });
</script>
</body>
</html>
'''

FILES["app/templates/error.html"] = r'''{% extends "base.html" %}
{% block title %}{{ status_code }} {{ title }}{% endblock %}
{% block content %}
<h2>{{ title }}</h2>
<div class="errors">{{ detail }}</div>
<p><a href="/">New report</a> | <a href="/equipment">History</a></p>
{% endblock %}
'''

FILES["tests/test_logging.py"] = r'''import json
import logging

import app.ai as ai_mod
from app.ai import AIError
from app.logging_setup import JsonFormatter
from tests.test_ai import GOOD
from tests.test_reports import make_client

FORM = {
    "equipment_type": "pump", "identifier": "P-700",
    "description": "high bearing temperature", "recent_events": "Restarted yesterday", "temp_c": "98",
}


def events(caplog):
    return {getattr(r, "event", None) for r in caplog.records}


def test_json_formatter_outputs_structured_fields():
    logger = logging.getLogger("t")
    record = logger.makeRecord("t", logging.INFO, "f", 1, "hello", (), None, extra={"event": "x", "report_id": 5})
    data = json.loads(JsonFormatter().format(record))
    assert data["event"] == "x" and data["report_id"] == 5
    assert data["level"] == "INFO" and "ts" in data


def test_report_and_ai_workflow_events_are_logged(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(ai_mod, "call_llm", lambda system, prompt: json.dumps(GOOD))
    client = make_client()
    client.post("/reports", data=FORM, follow_redirects=False)
    client.post("/reports/1/analyze", follow_redirects=False)
    found = events(caplog)
    assert {"report_created", "retrieval_ok", "ai_analysis_started", "ai_output_accepted", "ai_analysis_ok"} <= found


def test_ai_failure_is_logged_as_warning(monkeypatch, caplog):
    caplog.set_level(logging.INFO)

    def boom(system, prompt):
        raise AIError("The AI service timed out. Try again.")

    monkeypatch.setattr(ai_mod, "call_llm", boom)
    client = make_client()
    client.post("/reports", data=FORM, follow_redirects=False)
    client.post("/reports/1/analyze", follow_redirects=False)
    failed = [r for r in caplog.records if getattr(r, "event", "") == "ai_analysis_failed"]
    assert failed and failed[0].levelno == logging.WARNING
    assert "timed out" in failed[0].error


def test_technician_decisions_are_logged(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(ai_mod, "call_llm", lambda system, prompt: json.dumps(GOOD))
    client = make_client()
    client.post("/reports", data=FORM, follow_redirects=False)
    client.post("/reports/1/analyze", follow_redirects=False)
    client.post("/work-orders/1/approve", data={"note": "ok"}, follow_redirects=False)
    assert "work_order_approved" in events(caplog)
'''

FILES["tests/test_pages.py"] = r'''import json

import app.ai as ai_mod
from tests.test_ai import GOOD
from tests.test_reports import make_client

FORM = {"equipment_type": "pump", "identifier": "P-800", "description": "high bearing temperature", "temp_c": "98"}


def test_success_notice_after_report_submission():
    client = make_client()
    r = client.post("/reports", data=FORM, follow_redirects=False)
    assert "notice=report_created" in r.headers["location"]
    assert "Report submitted" in client.get(r.headers["location"]).text


def test_unknown_notice_is_ignored():
    client = make_client()
    client.post("/reports", data=FORM, follow_redirects=False)
    page = client.get("/reports/1?notice=<script>alert(1)</script>")
    assert "<script>alert(1)</script>" not in page.text


def test_analysis_button_has_loading_state():
    client = make_client()
    client.post("/reports", data=FORM, follow_redirects=False)
    assert "data-loading" in client.get("/reports/1").text


def test_not_found_page_is_friendly_html_and_api_stays_json():
    client = make_client()
    page = client.get("/reports/999")
    assert page.status_code == 404
    assert "text/html" in page.headers["content-type"]
    assert "Report not found" in page.text
    api = client.get("/api/reports/999")
    assert api.status_code == 404
    assert api.json()["detail"] == "Report not found"


def test_locked_work_order_shows_clear_error_page(monkeypatch):
    monkeypatch.setattr(ai_mod, "call_llm", lambda system, prompt: json.dumps(GOOD))
    client = make_client()
    client.post("/reports", data=FORM, follow_redirects=False)
    client.post("/reports/1/analyze", follow_redirects=False)
    client.post("/work-orders/1/approve", follow_redirects=False)
    r = client.post("/work-orders/1/approve", follow_redirects=False)
    assert r.status_code == 409
    assert "already approved" in r.text


def test_empty_history_state():
    client = make_client()
    assert "No equipment yet" in client.get("/equipment").text
'''

FILES["README.md"] = r'''# Equipment Maintenance Triage Assistant

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
'''

FILES["AGENT_USAGE.md"] = r'''# Agent usage

## Tools

- **Claude (chat)**: main coding assistant. It drafted the code, tests and documentation; I ran, tested and committed everything.
- **VS Code, Git and GitHub**: editing, small commits and the public repository.
- **Render**: hosting. **Google AI Studio**: free Gemini API key for the in-app AI step.
- **Gemini (`gemini-3.5-flash`)** is the LLM inside the app. It is part of the product, not a development tool.

## How I worked

I pasted the assignment and both problem statements and asked the assistant to read them carefully. It recommended the medium problem (Equipment Maintenance Triage Assistant) because a complete, reliable medium solution can score higher than a half-finished expert one, and I agreed. I then built it one step at a time, with small commits after each step:

1. Project setup, database models and the knowledge base
2. Report form, API and deterministic threshold rules
3. Manual retrieval (BM25) and retrieval failure handling
4. AI analysis with JSON validation and citations
5. Work order edit/approve/reject, confirmed findings, equipment history and AI retry
6. Demo seed data, README and deployment
7. Structured logging, UI states, error pages and documentation

## Representative prompts (paraphrased)

- "Read the whole assignment carefully and tell me which problem to pick and how to plan it."
- "Let's go one step at a time, move fast, follow every instruction and tell me when to commit."
- "Give me everything in a single file so I can open it directly in VS Code."
- "I want multiple small commits with proper messages, not one big commit." (after the first commit block bundled everything)
- I pasted terminal errors and screenshots directly (Python not found, API 404, API 503, missing key) and asked how to fix them.

## What was delegated and what I did

- **Drafted by the assistant**: application code, rules engine, retrieval, AI prompt and validation, templates, tests, README and deployment instructions.
- **Done by me**: choosing the problem and scope, installing and running everything on Windows, getting the API key, debugging environment problems, pushing back on process mistakes, running the tests, committing, deploying to Render and testing the live site.

## Agent mistakes and rejected suggestions

- **Windows setup commands were wrong at first.** The first instructions used `python` and a Command Prompt activation command, but my machine used PowerShell and the Microsoft Store alias blocked `python`. Fixed by using `py` and `Activate.ps1`.
- **Git instructions were incomplete.** The first commit block saved locally but never connected the GitHub remote or pushed, and it bundled everything into one commit. I pointed this out, and the process was changed to a remote push plus several small commits.
- **Wrong default Gemini model.** The default model name returned a 404 for my key. I found a working model and made the model name configurable with `GEMINI_MODEL`.
- **No handling of temporary AI errors.** The first AI version failed outright on a 503 overload. After seeing it happen, retry with short delays for 429, 503 and timeouts was added, with tests.
- **I removed my own API key line from `.env` by accident** while adding the model name. The app showed a clear "GEMINI_API_KEY is not set" error, which confirmed the failure message works, and I restored the key.
- **Rejected the expert problem** (migration workbench): too many moving parts for the time available.
- **Rejected a vector database** for retrieval: BM25 over small sections is simpler, transparent and testable.
- **Rejected trusting the AI's priority and citations as-is**: a deterministic priority floor and code-level citation checks were added instead.
- **Critical limits are not in the manuals.** The manuals only give warning levels, so the critical limits are my own assumptions, and the README says so.

## How I verified the output

- Ran the automated test suite with `pytest` after each step. The AI is mocked in tests, so they cover validation, retries, failures, the approval flow and locking, history, logging and UI states.
- Ran the app locally and checked the key cases by hand: critical threshold, conflicting data, missing readings and a real Gemini analysis with citations and a draft work order.
- Checked that the AI cannot approve work or create confirmed findings: those routes are technician-only, and tests assert that a new work order is always a draft.
- Checked secrets: `.env` is in `.gitignore`, `git status` never listed it, and the key exists only locally and in Render's environment settings.
- Deployed to Render and opened the live site to check the form, history and demo data.
'''


def patch(path, replacements, marker):
    if not os.path.exists(path):
        print(f"WARNING: {path} not found. Run from the triage folder.")
        return
    with open(path, encoding="utf-8") as f:
        s = f.read()
    if marker in s:
        print(f"{path} already patched")
        return
    for old, new in replacements:
        if old not in s:
            print(f"WARNING: could not patch {path} (text not found: {old.strip()[:60]!r}). Tell me and I will give the manual edit.")
            return
        s = s.replace(old, new, 1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(s)
    print(f"patched: {path}")


def main():
    for path, content in FILES.items():
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"written: {path}")

    patch("app/ai.py", [
        ("from pydantic import BaseModel, Field, ValidationError\n",
         "from pydantic import BaseModel, Field, ValidationError\n\nfrom .logging_setup import log_event\n"),
        ('logger.warning("AI output rejected (attempt %d): %s", attempt, last_error)',
         'log_event(logger, "ai_output_rejected", logging.WARNING, attempt=attempt, reason=last_error)'),
        ('logger.warning("AI request timed out, retrying (%d/%d)", i + 1, attempts - 1)',
         'log_event(logger, "ai_http_timeout_retry", logging.WARNING, attempt=i + 1, max_retries=attempts - 1)'),
        ('logger.warning("AI service returned %s, retrying (%d/%d)", resp.status_code, i + 1, attempts - 1)',
         'log_event(logger, "ai_http_retry", logging.WARNING, status_code=resp.status_code, attempt=i + 1, max_retries=attempts - 1)'),
        ('            resp = httpx.post(url, headers={"x-goog-api-key": key}, json=body, timeout=60)\n',
         '            started = time.monotonic()\n'
         '            resp = httpx.post(url, headers={"x-goog-api-key": key}, json=body, timeout=60)\n'
         '            log_event(logger, "ai_http_attempt", attempt=i + 1, status_code=resp.status_code,\n'
         '                      latency_ms=int((time.monotonic() - started) * 1000), model=model_name())\n'),
        ('    level, raised = resolve_priority(analysis.priority.level, observations)\n',
         '    log_event(logger, "ai_output_accepted", attempts=attempt, causes=len(analysis.possible_causes))\n'
         '    level, raised = resolve_priority(analysis.priority.level, observations)\n'),
    ], "log_event")

    patch("app/templates/report.html", [
        ('<form method="post" action="/reports/{{ report.id }}/analyze">',
         '<form method="post" action="/reports/{{ report.id }}/analyze" data-loading="Analyzing... this can take up to a minute">'),
    ], "data-loading")

    # make sure the working model name is the default everywhere
    for path in ("app/ai.py", ".env.example"):
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                s = f.read()
            if "gemini-2.5-flash" in s:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(s.replace("gemini-2.5-flash", "gemini-3.5-flash"))
                print(f"updated default model in {path}")

    print("\nDone. Next:")
    print("  pytest")
    print("  uvicorn app.main:app --reload")


if __name__ == "__main__":
    main()
