"""
Step 2: report form + API + deterministic threshold rules + tests.
Run from the triage folder (venv active, server stopped):  python setup_step2.py
Overwrites app/main.py and adds rules, thresholds, templates and tests.
"""
import os
import re

FILES = {}

FILES["app/thresholds.json"] = r'''{
  "pump": {
    "manual": "pump",
    "sensors": {
      "temp_c": {"label": "Bearing temperature", "unit": "C", "warn_max": 85, "critical_max": 95, "manual_section": 1},
      "vibration_mm_s": {"label": "Vibration", "unit": "mm/s", "warn_max": 4.5, "critical_max": 7.0, "manual_section": 1},
      "pressure_bar": {"label": "Discharge pressure", "unit": "bar", "min_ok": 4, "warn_max": 6, "manual_section": 1}
    }
  },
  "motor": {
    "manual": "motor",
    "sensors": {
      "temp_c": {"label": "Winding temperature", "unit": "C", "warn_max": 120, "critical_max": 140, "manual_section": 1},
      "vibration_mm_s": {"label": "Vibration", "unit": "mm/s", "warn_max": 3.5, "critical_max": 6.0, "manual_section": 1},
      "current_dev_pct": {"label": "Current deviation from rating", "unit": "%", "abs": true, "warn_max": 10, "critical_max": 25, "manual_section": 1}
    }
  },
  "compressor": {
    "manual": "compressor",
    "sensors": {
      "temp_c": {"label": "Discharge air temperature", "unit": "C", "warn_max": 110, "critical_max": 120, "manual_section": 1},
      "pressure_bar": {"label": "Discharge pressure", "unit": "bar", "min_ok": 7, "warn_max": 8, "manual_section": 1},
      "oil_pressure_bar": {"label": "Oil pressure", "unit": "bar", "min_ok": 2, "warn_max": 4, "manual_section": 1},
      "vibration_mm_s": {"label": "Vibration", "unit": "mm/s", "warn_max": 5.0, "critical_max": 8.0, "manual_section": 1}
    }
  },
  "conveyor": {
    "manual": "conveyor",
    "sensors": {
      "temp_c": {"label": "Gearbox temperature", "unit": "C", "warn_max": 95, "critical_max": 110, "manual_section": 1},
      "current_dev_pct": {"label": "Drive current deviation from rating", "unit": "%", "abs": true, "warn_max": 10, "critical_max": 25, "manual_section": 1},
      "speed_dev_pct": {"label": "Belt speed deviation", "unit": "%", "abs": true, "warn_max": 5, "critical_max": 10, "manual_section": 1}
    }
  }
}
'''

FILES["app/rules.py"] = r'''"""Deterministic threshold checks. No AI involved. Limits live in thresholds.json."""
import json
import math
import re
from pathlib import Path

THRESHOLDS = json.loads((Path(__file__).parent / "thresholds.json").read_text(encoding="utf-8"))

SENSOR_LABELS = {
    "temp_c": "Temperature (C)",
    "vibration_mm_s": "Vibration (mm/s RMS)",
    "pressure_bar": "Discharge pressure (bar)",
    "oil_pressure_bar": "Oil pressure (bar)",
    "current_dev_pct": "Current deviation from rating (%)",
    "speed_dev_pct": "Belt speed deviation (%)",
}

# Readings outside these ranges are treated as invalid (likely sensor fault or typo)
PLAUSIBLE = {
    "temp_c": (-50, 500),
    "vibration_mm_s": (0, 100),
    "pressure_bar": (0, 100),
    "oil_pressure_bar": (0, 50),
    "current_dev_pct": (-100, 500),
    "speed_dev_pct": (-100, 500),
}

# If the description mentions the symptom but the sensor reads normal -> conflict
CONFLICT_PATTERNS = {
    "temp_c": r"overheat|too hot|\bhot\b|burning|high temp",
    "vibration_mm_s": r"vibrat|shak|rattl|noisy|noise",
    "pressure_bar": r"low pressure|pressure drop|not building pressure",
}


def _item(severity, text, source):
    return {"severity": severity, "text": text, "source": source}


def classify(value, rule):
    """Return 'critical', 'warning' or 'ok' for one reading."""
    v = abs(value) if rule.get("abs") else value
    if "critical_max" in rule and v > rule["critical_max"]:
        return "critical"
    if "warn_max" in rule and v > rule["warn_max"]:
        return "warning"
    if "min_ok" in rule and v < rule["min_ok"]:
        return "warning"
    return "ok"


def _describe(status, value, rule, ref):
    label, unit = rule["label"], rule["unit"]
    shown = abs(value) if rule.get("abs") else value
    if status == "critical":
        return f"{label}: {value} {unit} is above the critical limit of {rule['critical_max']} {unit} (manual {ref})."
    if status == "warning":
        if "min_ok" in rule and shown < rule["min_ok"]:
            return f"{label}: {value} {unit} is below the minimum of {rule['min_ok']} {unit} (manual {ref})."
        return f"{label}: {value} {unit} is above the warning limit of {rule['warn_max']} {unit} (manual {ref})."
    return f"{label}: {value} {unit} is within normal limits."


def check_sensors(equipment_type, sensors, description=""):
    """Run all deterministic checks. Returns a list of {severity, text, source}.
    severity: critical | warning | ok | data_quality | info"""
    cfg = THRESHOLDS.get(equipment_type)
    if cfg is None:
        return [_item("data_quality", f"Unknown equipment type '{equipment_type}'. No threshold rules applied.", "system")]
    if not sensors:
        return [_item("info", "No sensor readings provided. Threshold checks skipped.", "sensor:none")]

    expected = cfg["sensors"]
    out = []
    statuses = {}

    for key, rule in expected.items():
        src = f"sensor:{key}"
        ref = f"{cfg['manual']}:{rule['manual_section']}"
        value = sensors.get(key)
        if value is None:
            out.append(_item("data_quality", f"{rule['label']}: reading is missing, so this limit could not be checked.", src))
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            out.append(_item("data_quality", f"{rule['label']}: invalid reading '{value}'. Not used for threshold checks.", src))
            continue
        lo, hi = PLAUSIBLE[key]
        if not lo <= value <= hi:
            out.append(_item("data_quality", f"{rule['label']}: reading {value} {rule['unit']} is outside the plausible range ({lo} to {hi}). Possible sensor fault or typo.", src))
            continue
        status = classify(value, rule)
        statuses[key] = status
        out.append(_item(status, _describe(status, value, rule, ref), src))

    for key in sensors:
        if key not in expected:
            out.append(_item("info", f"Reading '{key}' is not checked for {equipment_type} equipment (no rule defined).", f"sensor:{key}"))

    text = (description or "").lower()
    for key, pattern in CONFLICT_PATTERNS.items():
        if statuses.get(key) == "ok" and re.search(pattern, text):
            label = expected[key]["label"]
            out.append(_item(
                "data_quality",
                f"Conflict: the description suggests a {label.lower()} problem, but the sensor reads normal. "
                "Possible sensor fault, stale reading or intermittent issue. Verify on site.",
                f"sensor:{key}",
            ))
    return out
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
  .placeholder { border: 1px dashed #aaa; padding: 12px; color: #666; border-radius: 4px; }
</style>
</head>
<body>
<header>
  <b>Equipment Maintenance Triage Assistant</b>
  <a href="/">New report</a>
</header>
<div class="notice">Decision support only. This system never controls equipment and never approves maintenance work automatically. A technician must review and approve.</div>
<main>
{% block content %}{% endblock %}
</main>
</body>
</html>
'''

FILES["app/templates/new_report.html"] = r'''{% extends "base.html" %}
{% block title %}New report{% endblock %}
{% block content %}
<h2>Report an equipment problem</h2>
{% if errors %}
<div class="errors"><ul>{% for e in errors %}<li>{{ e }}</li>{% endfor %}</ul></div>
{% endif %}
<form method="post" action="/reports">
  <div class="grid">
    <div>
      <label>Equipment type</label>
      <select name="equipment_type">
        {% for t in types %}<option value="{{ t }}" {% if values.get('equipment_type') == t %}selected{% endif %}>{{ t|capitalize }}</option>{% endfor %}
      </select>
    </div>
    <div>
      <label>Equipment identifier</label>
      <input name="identifier" placeholder="e.g. P-101" value="{{ values.get('identifier', '') }}">
    </div>
  </div>

  <label>Issue description</label>
  <textarea name="description" rows="4" placeholder="What is wrong? What did you see, hear or smell?">{{ values.get('description', '') }}</textarea>

  <label>Recent operating events (one per line)</label>
  <textarea name="recent_events" rows="4" placeholder="e.g. Restarted after maintenance yesterday&#10;Load increased to 90% this morning">{{ values.get('recent_events', '') }}</textarea>

  <h3>Sensor readings (optional)</h3>
  <p class="muted">Leave blank if not available. Only readings relevant to the equipment type are checked.</p>
  <div class="grid">
    {% for key, label in sensor_labels.items() %}
    <div>
      <label>{{ label }}</label>
      <input name="{{ key }}" value="{{ values.get(key, '') }}">
    </div>
    {% endfor %}
  </div>

  <button type="submit">Submit report</button>
</form>
{% endblock %}
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

<h3>AI analysis</h3>
<div class="placeholder">Not run yet. (Added in the next step: possible causes, follow-up questions, inspection steps, priority and draft work order, all with citations.)</div>
{% endblock %}
'''

FILES["app/main.py"] = r'''import math
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .db import Base, engine, get_db
from .rules import SENSOR_LABELS, THRESHOLDS, check_sensors

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

    for item in check_sensors(etype, sensors or None, desc):
        db.add(models.Finding(
            report_id=report.id, kind="observation", origin="rule",
            text=item["text"], source=item["source"], severity=item["severity"],
        ))
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
        "findings": [
            {"kind": f.kind, "origin": f.origin, "severity": f.severity, "text": f.text, "source": f.source}
            for f in report.findings
        ],
    }
'''

FILES["tests/test_rules.py"] = r'''from app.rules import check_sensors


def sev(items, key):
    return [i["severity"] for i in items if i["source"] == f"sensor:{key}"]


def test_critical_temperature():
    items = check_sensors("pump", {"temp_c": 100.0})
    assert "critical" in sev(items, "temp_c")


def test_warning_temperature():
    items = check_sensors("pump", {"temp_c": 90.0})
    assert "warning" in sev(items, "temp_c")


def test_normal_temperature():
    items = check_sensors("pump", {"temp_c": 70.0})
    assert sev(items, "temp_c") == ["ok"]


def test_low_pressure_is_warning():
    items = check_sensors("pump", {"pressure_bar": 2.0})
    assert "warning" in sev(items, "pressure_bar")


def test_negative_current_deviation_uses_absolute_value():
    items = check_sensors("motor", {"current_dev_pct": -30.0})
    assert "critical" in sev(items, "current_dev_pct")


def test_missing_reading_is_flagged():
    items = check_sensors("pump", {"temp_c": 70.0})
    assert "data_quality" in sev(items, "vibration_mm_s")


def test_invalid_reading_is_flagged_not_checked():
    items = check_sensors("pump", {"temp_c": "abc"})
    assert sev(items, "temp_c") == ["data_quality"]


def test_implausible_reading_is_flagged():
    items = check_sensors("pump", {"temp_c": 9999.0})
    assert sev(items, "temp_c") == ["data_quality"]


def test_conflict_description_vs_sensor():
    items = check_sensors("pump", {"temp_c": 60.0}, "The pump is overheating")
    assert any(i["severity"] == "data_quality" and "Conflict" in i["text"] for i in items)


def test_no_conflict_when_sensor_confirms():
    items = check_sensors("pump", {"temp_c": 100.0}, "The pump is overheating")
    assert not any("Conflict" in i["text"] for i in items)


def test_no_sensors_gives_info():
    items = check_sensors("pump", None)
    assert items[0]["severity"] == "info"


def test_unknown_equipment_type():
    items = check_sensors("rocket", {"temp_c": 10.0})
    assert items[0]["severity"] == "data_quality"
'''

FILES["tests/test_reports.py"] = r'''from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app


def make_client():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng, autoflush=False)

    def override():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    return TestClient(app)


def test_submit_report_and_view_results():
    client = make_client()
    r = client.post("/reports", data={
        "equipment_type": "pump", "identifier": "P-101",
        "description": "Pump running hot", "recent_events": "Restarted yesterday",
        "temp_c": "98",
    }, follow_redirects=False)
    assert r.status_code == 303
    page = client.get(r.headers["location"])
    assert page.status_code == 200
    assert "CRITICAL" in page.text
    api = client.get("/api/reports/1").json()
    assert api["equipment"]["identifier"] == "P-101"
    assert api["recent_events"] == ["Restarted yesterday"]


def test_missing_required_fields_rejected():
    client = make_client()
    r = client.post("/reports", data={"equipment_type": "pump", "identifier": "", "description": ""})
    assert r.status_code == 400


def test_identifier_type_mismatch_rejected():
    client = make_client()
    base = {"identifier": "X-1", "description": "problem"}
    client.post("/reports", data={**base, "equipment_type": "pump"}, follow_redirects=False)
    r = client.post("/reports", data={**base, "equipment_type": "motor"})
    assert r.status_code == 400


def test_unknown_report_returns_404():
    client = make_client()
    assert client.get("/reports/999").status_code == 404
'''


def patch_models():
    path = "app/models.py"
    if not os.path.exists(path):
        print("WARNING: app/models.py not found. Run from the triage folder.")
        return
    with open(path, encoding="utf-8") as f:
        s = f.read()
    if "severity" in s:
        print("models.py already has severity")
        return
    new, n = re.subn(
        r"(    source: Mapped\[str\] = mapped_column\(String\(100\)\).*\n)",
        r"\1    severity: Mapped[str | None] = mapped_column(String(20), nullable=True)  # critical/warning/ok/data_quality/info\n",
        s, count=1,
    )
    if n == 0:
        print("WARNING: could not patch models.py. Add this line to class Finding, after 'source':")
        print('    severity: Mapped[str | None] = mapped_column(String(20), nullable=True)')
        return
    with open(path, "w", encoding="utf-8") as f:
        f.write(new)
    print("patched: app/models.py (added Finding.severity)")


def main():
    for path, content in FILES.items():
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"written: {path}")
    patch_models()
    # The DB schema changed (new column). Dev database is disposable, so recreate it.
    try:
        if os.path.exists("triage.db"):
            os.remove("triage.db")
            print("removed old triage.db (will be recreated on start)")
    except OSError:
        print("Could not delete triage.db. Stop the server (Ctrl+C) and delete triage.db manually, then rerun.")
    print("\nDone. Next:")
    print("  pytest")
    print("  uvicorn app.main:app --reload")
    print("  open http://127.0.0.1:8000")


if __name__ == "__main__":
    main()
