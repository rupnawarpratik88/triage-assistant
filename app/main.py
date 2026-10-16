import logging
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
