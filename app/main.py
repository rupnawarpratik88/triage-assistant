import logging
import math
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .ai import AIError, analyze
from .db import Base, engine, get_db
from .retrieval import RetrievalError, retrieve
from .workflow import router as workflow_router
from .rules import SENSOR_LABELS, THRESHOLDS, check_sensors

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("triage")

Base.metadata.create_all(engine)
app = FastAPI(title="Equipment Maintenance Triage Assistant")
app.include_router(workflow_router)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _events(report):
    return [e.strip() for e in (report.recent_events or "").split("\n") if e.strip()]


def _redirect(report_id):
    return RedirectResponse(f"/reports/{report_id}", status_code=303)


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
    return _redirect(report.id)


@app.post("/reports/{report_id}/analyze")
def analyze_report(report_id: int, db: Session = Depends(get_db)):
    report = db.get(models.Report, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    if report.ai_status == "ok":
        return _redirect(report_id)

    def fail(message):
        report.ai_status = "failed"
        report.ai_error = message
        db.add(models.AuditLog(entity="report", entity_id=report.id, action="ai_analysis_failed",
                               actor="ai", detail={"error": message}))
        db.commit()
        return _redirect(report_id)

    if report.retrieval_error or not report.retrieved:
        return fail("AI analysis needs manual evidence, but retrieval failed for this report.")

    observations = [{"severity": f.severity, "text": f.text, "source": f.source}
                    for f in report.findings if f.origin == "rule"]
    try:
        result = analyze(report.equipment.type, report.equipment.identifier, report.description,
                         _events(report), observations, report.retrieved)
    except AIError as exc:
        logger.warning("report %s: AI analysis failed: %s", report.id, exc)
        return fail(str(exc))
    except Exception as exc:
        logger.exception("report %s: unexpected AI error", report.id)
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
    logger.info("report %s: AI analysis ok, draft work order %s", report.id, order.id)
    return _redirect(report_id)


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
