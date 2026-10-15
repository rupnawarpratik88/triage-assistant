"""
Step 5: work order edit / approve / reject with audit history, technician-only confirmed findings,
equipment history pages, automatic retry for temporary AI errors (429/503/timeouts), tests.
Run from the triage folder (venv active, server stopped):  python setup_step5.py
Adds new files, overwrites base.html and report.html, patches app/ai.py and app/main.py.
No database reset needed.
"""
import os

FILES = {}

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


def _redirect(wo_id):
    return RedirectResponse(f"/work-orders/{wo_id}", status_code=303)


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
        return _page(request, db, wo, errors, 400)
    changes = {}
    for field, new in (("title", title), ("body", body), ("priority", priority)):
        old = getattr(wo, field)
        if old != new:
            changes[field] = {"before": old, "after": new}
            setattr(wo, field, new)
    if changes:
        _audit(db, "work_order", wo.id, "edited", {"changes": changes})
        logger.info("work order %s edited: %s", wo.id, ", ".join(changes))
    db.commit()
    return _redirect(wo.id)


async def _decide(wo_id, request, db, new_status):
    wo = _get_work_order(db, wo_id)
    if wo.status != "draft":
        raise HTTPException(status_code=409, detail=f"Work order is already {wo.status}.")
    form = await request.form()
    note = str(form.get("note", "")).strip()
    wo.status = new_status
    _audit(db, "work_order", wo.id, new_status, {"note": note} if note else None)
    db.commit()
    logger.info("work order %s %s by technician", wo.id, new_status)
    return _redirect(wo.id)


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
    return RedirectResponse(f"/reports/{report.id}", status_code=303)


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
<title>{% block title %}Triage Assistant{% endblock %}</title>
<style>
  body { font-family: Arial, sans-serif; margin: 0; background: #f5f6f8; color: #222; }
  header { background: #1f3a5f; color: #fff; padding: 12px 24px; display: flex; gap: 24px; align-items: center; }
  header a { color: #cfe3ff; text-decoration: none; }
  .notice { background: #fff4d6; border-bottom: 1px solid #e6cf8a; padding: 8px 24px; font-size: 13px; }
  main { max-width: 860px; margin: 24px auto; background: #fff; padding: 24px; border-radius: 8px; }
  label { display: block; margin-top: 12px; font-weight: bold; font-size: 14px; }
  input, select, textarea { width: 100%; padding: 8px; margin-top: 4px; box-sizing: border-box; }
  button { margin-top: 16px; padding: 10px 18px; background: #1f3a5f; color: #fff; border: 0; border-radius: 4px; cursor: pointer; }
  button.danger { background: #b23a3a; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0 16px; }
  .muted { color: #777; }
  .errors { background: #fde2e2; border: 1px solid #e29a9a; padding: 8px 16px; border-radius: 4px; }
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
{% block content %}{% endblock %}
</main>
</body>
</html>
'''

FILES["app/templates/report.html"] = r'''{% extends "base.html" %}
{% block title %}Report #{{ report.id }}{% endblock %}
{% block content %}
<h2>Report #{{ report.id }}: {{ report.equipment.type|capitalize }} {{ report.equipment.identifier }}</h2>
<p class="muted">Submitted {{ report.created_at.strftime("%Y-%m-%d %H:%M") }} UTC |
  <a href="/equipment/{{ report.equipment.identifier }}">Equipment history</a></p>

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

{% set rule_findings = report.findings|selectattr("origin", "equalto", "rule")|list %}
{% set flags = rule_findings|selectattr("severity", "equalto", "data_quality")|list %}
{% set checks = rule_findings|rejectattr("severity", "equalto", "data_quality")|list %}

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
{% if report.ai_status == "ok" and report.ai_output %}
  {% set ai = report.ai_output %}
  <p class="muted">Generated by {{ ai.model }}. Everything below is a suggestion, not a confirmed finding. A technician must verify it.</p>

  <h4>Possible causes (unconfirmed)</h4>
  {% for f in report.findings|selectattr("kind", "equalto", "possible_cause") %}
  <div class="item warning"><span class="badge possible">POSSIBLE</span> {{ f.text }}<br>
    {% for c in f.source.split(",") %}<span class="tag">{{ c }}</span> {% endfor %}</div>
  {% endfor %}

  <h4>Follow-up questions</h4>
  {% if ai.follow_up_questions %}
    {% for q in ai.follow_up_questions %}
    <div class="item info">{{ q.text }}<br>{% for c in q.citations %}<span class="tag">{{ c }}</span> {% endfor %}</div>
    {% endfor %}
  {% else %}
    <p class="muted">None.</p>
  {% endif %}

  <h4>Suggested inspection steps</h4>
  {% for s in ai.inspection_steps %}
  <div class="item info"><b>{{ loop.index }}.</b> {{ s.text }}<br>{% for c in s.citations %}<span class="tag">{{ c }}</span> {% endfor %}</div>
  {% endfor %}

  <h4>Suggested priority</h4>
  <div class="item {% if ai.priority.level in ['high', 'critical'] %}critical{% elif ai.priority.level == 'medium' %}warning{% else %}ok{% endif %}">
    <b>{{ ai.priority.level|upper }}</b>
    {% if ai.priority.raised_by_rules %}<span class="muted">(AI suggested {{ ai.priority.ai_level|upper }}; raised by the deterministic threshold rules)</span>{% endif %}<br>
    {{ ai.priority.rationale }}<br>
    {% for c in ai.priority.citations %}<span class="tag">{{ c }}</span> {% endfor %}
  </div>

  <h4>Draft work order</h4>
  {% for w in report.work_orders %}
    <p><b>{{ w.title }}</b> <span class="badge">{{ w.status|upper }}</span>
      <a href="/work-orders/{{ w.id }}">Edit / approve / reject</a></p>
    <pre class="wo">{{ w.body }}</pre>
  {% endfor %}
  <p class="muted">Nothing is approved automatically. A technician decides on the work order page.</p>

{% else %}
  {% if report.ai_status == "failed" %}
  <div class="errors"><b>AI analysis failed:</b> {{ report.ai_error }}
    The deterministic checks and manual sections above are still valid.</div>
  {% endif %}
  {% if report.retrieval_error %}
    <p class="muted">AI analysis is unavailable because retrieval failed.</p>
  {% else %}
    <form method="post" action="/reports/{{ report.id }}/analyze">
      <button type="submit">{% if report.ai_status == "failed" %}Retry AI analysis{% else %}Run AI analysis{% endif %}</button>
      <span class="muted">Takes about 5-30 seconds. Report text is sent to the AI service.</span>
    </form>
  {% endif %}
{% endif %}

<h3>Confirmed findings (technician only)</h3>
<p class="muted">Only a technician can record a confirmed finding. AI suggestions stay "possible" until you confirm them here.</p>
{% for f in report.findings|selectattr("kind", "equalto", "confirmed_finding") %}
  <div class="item ok"><span class="badge">CONFIRMED</span> {{ f.text }}
    <span class="muted">{{ f.created_at.strftime("%Y-%m-%d %H:%M") }} UTC</span></div>
{% else %}
  <p class="muted">None recorded yet.</p>
{% endfor %}
<form method="post" action="/reports/{{ report.id }}/findings">
  <label>Record a confirmed finding (what you verified on site)</label>
  <textarea name="text" rows="2"></textarea>
  <button type="submit">Record confirmed finding</button>
</form>
{% endblock %}
'''

FILES["app/templates/work_order.html"] = r'''{% extends "base.html" %}
{% block title %}Work order #{{ wo.id }}{% endblock %}
{% block content %}
<h2>Work order #{{ wo.id }} <span class="badge">{{ wo.status|upper }}</span></h2>
<p><a href="/reports/{{ report.id }}">Report #{{ report.id }}</a> |
  <a href="/equipment/{{ report.equipment.identifier }}">{{ report.equipment.type|capitalize }} {{ report.equipment.identifier }} history</a></p>

{% if errors %}
<div class="errors"><ul>{% for e in errors %}<li>{{ e }}</li>{% endfor %}</ul></div>
{% endif %}

{% if wo.status == "draft" %}
  <p class="muted">AI-drafted work order. Edit it, save, then approve or reject. Nothing is approved automatically.</p>
  <form method="post" action="/work-orders/{{ wo.id }}/edit">
    <label>Title</label>
    <input name="title" value="{{ wo.title }}">
    <label>Priority</label>
    <select name="priority">
      {% for p in priorities %}<option value="{{ p }}" {% if wo.priority == p %}selected{% endif %}>{{ p|capitalize }}</option>{% endfor %}
    </select>
    <label>Details</label>
    <textarea name="body" rows="16">{{ wo.body }}</textarea>
    <button type="submit">Save changes</button>
  </form>

  <h3>Decision</h3>
  <p class="muted">Save your changes first, then decide. Approved and rejected work orders are locked.</p>
  <form method="post" action="/work-orders/{{ wo.id }}/approve">
    <label>Note (optional)</label>
    <input name="note">
    <button type="submit">Approve</button>
  </form>
  <form method="post" action="/work-orders/{{ wo.id }}/reject">
    <label>Reason (optional)</label>
    <input name="note">
    <button type="submit" class="danger">Reject</button>
  </form>
{% else %}
  <p class="muted">This work order is {{ wo.status }} and locked.</p>
  <h3>{{ wo.title }}</h3>
  <p>Priority: <b>{{ wo.priority|upper }}</b></p>
  <pre class="wo">{{ wo.body }}</pre>
{% endif %}

<h3>History</h3>
{% for a in audit %}
  <div class="item info"><span class="tag">{{ a.at.strftime("%Y-%m-%d %H:%M:%S") }}</span>
    <b>{{ a.action }}</b> by {{ a.actor }}
    {% if a.detail %}<div class="muted">{{ a.detail|tojson }}</div>{% endif %}</div>
{% endfor %}
{% endblock %}
'''

FILES["app/templates/equipment_list.html"] = r'''{% extends "base.html" %}
{% block title %}Equipment history{% endblock %}
{% block content %}
<h2>Equipment history</h2>
{% for e in items %}
  <div class="item info"><a href="/equipment/{{ e.identifier }}"><b>{{ e.identifier }}</b></a>
    ({{ e.type|capitalize }}) <span class="muted">{{ e.reports|length }} report(s)</span></div>
{% else %}
  <p class="muted">No equipment yet. Submit a report first.</p>
{% endfor %}
{% endblock %}
'''

FILES["app/templates/equipment.html"] = r'''{% extends "base.html" %}
{% block title %}{{ eq.identifier }} history{% endblock %}
{% block content %}
<h2>{{ eq.type|capitalize }} {{ eq.identifier }}: history</h2>
<p class="muted">{{ reports|length }} report(s), newest first. <a href="/equipment">All equipment</a></p>
{% for r in reports %}
<div class="item info">
  <b><a href="/reports/{{ r.id }}">Report #{{ r.id }}</a></b>
  <span class="muted">{{ r.created_at.strftime("%Y-%m-%d %H:%M") }} UTC</span>
  {% if r.priority %}<span class="badge">PRIORITY {{ r.priority|upper }}</span>{% endif %}
  <span class="badge">AI: {{ r.ai_status }}</span><br>
  {{ r.description }}
  {% set confirmed = r.findings|selectattr("kind", "equalto", "confirmed_finding")|list %}
  {% set causes = r.findings|selectattr("kind", "equalto", "possible_cause")|list %}
  {% set obs = r.findings|selectattr("kind", "equalto", "observation")|list %}
  <div class="muted">{{ obs|length }} observation(s), {{ causes|length }} possible cause(s), {{ confirmed|length }} confirmed finding(s)</div>
  {% for f in confirmed %}<div><span class="badge">CONFIRMED</span> {{ f.text }}</div>{% endfor %}
  {% for w in r.work_orders %}
    <div>Work order: <a href="/work-orders/{{ w.id }}">{{ w.title }}</a> <span class="badge">{{ w.status|upper }}</span></div>
  {% endfor %}
</div>
{% else %}
  <p class="muted">No reports yet.</p>
{% endfor %}
{% endblock %}
'''

FILES["tests/conftest.py"] = r'''import pytest

import app.ai as ai


@pytest.fixture(autouse=True)
def no_retry_delay(monkeypatch):
    """Tests never wait between AI retries."""
    monkeypatch.setattr(ai, "RETRY_DELAYS", ())
'''

FILES["tests/test_retry.py"] = r'''import pytest

import app.ai as ai
from app.ai import AIError
from tests.test_ai import FakeResp

PAYLOAD = {"candidates": [{"content": {"parts": [{"text": "hello"}]}}]}


def setup(monkeypatch, responses):
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.setattr(ai, "RETRY_DELAYS", (0, 0))
    monkeypatch.setattr(ai.time, "sleep", lambda s: None)
    calls = []

    def post(*args, **kwargs):
        calls.append(1)
        r = responses[len(calls) - 1]
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(ai.httpx, "post", post)
    return calls


def test_503_and_429_are_retried_until_success(monkeypatch):
    calls = setup(monkeypatch, [FakeResp(503), FakeResp(429), FakeResp(200, PAYLOAD)])
    assert ai.call_llm("s", "p") == "hello"
    assert len(calls) == 3


def test_persistent_503_gives_clear_error(monkeypatch):
    calls = setup(monkeypatch, [FakeResp(503), FakeResp(503), FakeResp(503)])
    with pytest.raises(AIError) as exc:
        ai.call_llm("s", "p")
    assert "503" in str(exc.value)
    assert len(calls) == 3


def test_bad_request_is_not_retried(monkeypatch):
    calls = setup(monkeypatch, [FakeResp(400)])
    with pytest.raises(AIError):
        ai.call_llm("s", "p")
    assert len(calls) == 1


def test_timeout_is_retried(monkeypatch):
    calls = setup(monkeypatch, [ai.httpx.TimeoutException("slow"), FakeResp(200, PAYLOAD)])
    assert ai.call_llm("s", "p") == "hello"
    assert len(calls) == 2
'''

FILES["tests/test_workflow.py"] = r'''import json

import app.ai as ai_mod
from tests.test_ai import GOOD
from tests.test_analysis import create
from tests.test_reports import make_client


def analysed_client(monkeypatch):
    monkeypatch.setattr(ai_mod, "call_llm", lambda system, prompt: json.dumps(GOOD))
    client = make_client()
    url = create(client)
    client.post(url + "/analyze", follow_redirects=False)
    return client, url


def edit_data(**over):
    data = {"title": "Edited title", "priority": "medium", "body": "Edited body text"}
    data.update(over)
    return data


def test_ai_never_approves_or_confirms(monkeypatch):
    client, _ = analysed_client(monkeypatch)
    api = client.get("/api/reports/1").json()
    assert api["work_orders"][0]["status"] == "draft"
    assert [f["kind"] for f in api["findings"]].count("confirmed_finding") == 0


def test_technician_can_edit_draft(monkeypatch):
    client, _ = analysed_client(monkeypatch)
    r = client.post("/work-orders/1/edit", data=edit_data(), follow_redirects=False)
    assert r.status_code == 303
    api = client.get("/api/reports/1").json()
    assert api["work_orders"][0]["title"] == "Edited title"
    assert api["work_orders"][0]["priority"] == "medium"
    page = client.get("/work-orders/1")
    assert "edited" in page.text


def test_edit_validation(monkeypatch):
    client, _ = analysed_client(monkeypatch)
    assert client.post("/work-orders/1/edit", data=edit_data(title=""), follow_redirects=False).status_code == 400
    assert client.post("/work-orders/1/edit", data=edit_data(priority="urgent"), follow_redirects=False).status_code == 400


def test_approve_locks_the_work_order(monkeypatch):
    client, _ = analysed_client(monkeypatch)
    r = client.post("/work-orders/1/approve", data={"note": "Looks right"}, follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/api/reports/1").json()["work_orders"][0]["status"] == "approved"
    assert client.post("/work-orders/1/edit", data=edit_data(), follow_redirects=False).status_code == 409
    assert client.post("/work-orders/1/approve", follow_redirects=False).status_code == 409
    assert client.post("/work-orders/1/reject", follow_redirects=False).status_code == 409
    assert "Looks right" in client.get("/work-orders/1").text


def test_reject_records_reason(monkeypatch):
    client, _ = analysed_client(monkeypatch)
    client.post("/work-orders/1/reject", data={"note": "Not needed now"}, follow_redirects=False)
    assert client.get("/api/reports/1").json()["work_orders"][0]["status"] == "rejected"
    assert "Not needed now" in client.get("/work-orders/1").text


def test_unknown_work_order_is_404():
    client = make_client()
    assert client.get("/work-orders/999").status_code == 404


def test_technician_can_record_confirmed_finding(monkeypatch):
    client, url = analysed_client(monkeypatch)
    r = client.post(url + "/findings", data={"text": "Oil level was very low"}, follow_redirects=False)
    assert r.status_code == 303
    api = client.get("/api/reports/1").json()
    confirmed = [f for f in api["findings"] if f["kind"] == "confirmed_finding"]
    assert len(confirmed) == 1 and confirmed[0]["origin"] == "technician"
    assert "Oil level was very low" in client.get(url).text


def test_empty_confirmed_finding_is_rejected(monkeypatch):
    client, url = analysed_client(monkeypatch)
    assert client.post(url + "/findings", data={"text": " "}, follow_redirects=False).status_code == 400


def test_equipment_history_lists_all_reports():
    client = make_client()
    base = {"equipment_type": "pump", "identifier": "P-900", "description": "high bearing temperature"}
    client.post("/reports", data={**base, "temp_c": "98"}, follow_redirects=False)
    client.post("/reports", data={**base, "description": "vibration and noise"}, follow_redirects=False)
    page = client.get("/equipment/P-900")
    assert page.status_code == 200
    assert "Report #1" in page.text and "Report #2" in page.text
    assert "P-900" in client.get("/equipment").text
    assert client.get("/equipment/NOPE").status_code == 404
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
            print(f"WARNING: could not patch {path} (text not found: {old.strip()[:50]!r}). Tell me and I will give the manual edit.")
            return
        s = s.replace(old, new, 1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(s)
    print(f"patched: {path}")


RETRY_HELPER = '''RETRY_DELAYS = (2, 5)  # seconds to wait before retrying temporary errors (429, 503, timeouts)


def _post_with_retry(url, key, body):
    attempts = len(RETRY_DELAYS) + 1
    for i in range(attempts):
        last = i == attempts - 1
        try:
            resp = httpx.post(url, headers={"x-goog-api-key": key}, json=body, timeout=60)
        except httpx.TimeoutException:
            if last:
                raise AIError("The AI service timed out. Try again.")
            logger.warning("AI request timed out, retrying (%d/%d)", i + 1, attempts - 1)
            time.sleep(RETRY_DELAYS[i])
            continue
        except httpx.HTTPError as exc:
            raise AIError(f"Could not reach the AI service ({exc.__class__.__name__}).")
        if resp.status_code in (429, 503) and not last:
            logger.warning("AI service returned %s, retrying (%d/%d)", resp.status_code, i + 1, attempts - 1)
            time.sleep(RETRY_DELAYS[i])
            continue
        return resp


def call_llm(system, prompt):'''


def main():
    for path, content in FILES.items():
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"written: {path}")

    patch("app/ai.py", [
        ("import os\nimport re\n", "import os\nimport re\nimport time\n"),
        ("def call_llm(system, prompt):", RETRY_HELPER),
        (
            '    try:\n'
            '        resp = httpx.post(url, headers={"x-goog-api-key": key}, json=body, timeout=60)\n'
            '    except httpx.TimeoutException:\n'
            '        raise AIError("The AI service timed out. Try again.")\n'
            '    except httpx.HTTPError as exc:\n'
            '        raise AIError(f"Could not reach the AI service ({exc.__class__.__name__}).")\n',
            '    resp = _post_with_retry(url, key, body)\n',
        ),
    ], "RETRY_DELAYS")

    patch("app/main.py", [
        ("from .retrieval import RetrievalError, retrieve\n",
         "from .retrieval import RetrievalError, retrieve\nfrom .workflow import router as workflow_router\n"),
        ('app = FastAPI(title="Equipment Maintenance Triage Assistant")\n',
         'app = FastAPI(title="Equipment Maintenance Triage Assistant")\napp.include_router(workflow_router)\n'),
    ], "workflow_router")

    print("\nDone. Next:")
    print("  pytest")
    print("  uvicorn app.main:app --reload")


if __name__ == "__main__":
    main()
