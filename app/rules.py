"""Deterministic threshold checks. No AI involved. Limits live in thresholds.json."""
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
