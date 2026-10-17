"""Technician workflow: work order edit/approve/reject, confirmed findings, equipment history.
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
from .labels import humanize, register_filters
from .logging_setup import log_event

logger = logging.getLogger("triage.workflow")
router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
register_filters(templates)
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


@router.post("/reports/{report_id}/sensor-issue")
async def report_sensor_issue(report_id: int, request: Request, db: Session = Depends(get_db)):
    """Technician flags a sensor with missing, invalid or conflicting data as faulty or unavailable."""
    report = db.get(models.Report, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    form = await request.form()
    source = str(form.get("source", "")).strip()
    flagged = {f.source for f in report.findings
               if f.origin == "rule" and f.severity == "data_quality" and f.source.startswith("sensor:")}
    if source not in flagged:
        raise HTTPException(status_code=400, detail="That sensor has no data quality flag on this report.")
    already = any(f.origin == "technician" and f.kind == "observation" and f.source == source
                  for f in report.findings)
    if not already:
        finding = models.Finding(
            report_id=report.id, kind="observation", origin="technician", source=source, severity="info",
            text=f"Technician reported a sensor issue: {humanize(source)} is unavailable or unreliable.",
        )
        db.add(finding)
        db.flush()
        _audit(db, "finding", finding.id, "sensor_issue_reported", {"report_id": report.id, "source": source})
        db.commit()
        log_event(logger, "sensor_issue_reported", report_id=report.id, source=source, actor="technician")
    return RedirectResponse(f"/reports/{report.id}?notice=sensor_reported", status_code=303)
