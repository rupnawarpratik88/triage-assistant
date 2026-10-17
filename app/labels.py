"""Human-readable labels for technical ids shown in the UI and in work order text."""
import re

SENSOR_NAMES = {
    "temp_c": "Temperature sensor (°C)",
    "vibration_mm_s": "Vibration sensor (mm/s)",
    "pressure_bar": "Discharge pressure sensor (bar)",
    "oil_pressure_bar": "Oil pressure sensor (bar)",
    "current_dev_pct": "Motor current deviation (%)",
    "speed_dev_pct": "Belt speed deviation (%)",
}

_MANUAL = re.compile(r"(?:manual\s+)?\b(pump|motor|compressor|conveyor):(\d+)\b")
_EVENT = re.compile(r"\bevent:(\d+)\b")
_SENSOR = re.compile(r"\bsensor:([a-z_]+)\b")


def _sensor(match):
    key = match.group(1)
    if key == "none":
        return "Sensor readings"
    return SENSOR_NAMES.get(key, key.replace("_", " ").capitalize() + " sensor")


def humanize(text):
    """Replace ids such as pump:2, event:1, sensor:temp_c, report:description with readable labels."""
    if text is None:
        return ""
    text = str(text)
    text = text.replace("report:description", "Issue description")
    text = _SENSOR.sub(_sensor, text)
    text = _EVENT.sub(lambda m: f"Event {m.group(1)}", text)
    text = _MANUAL.sub(lambda m: f"{m.group(1).capitalize()} manual, section {m.group(2)}", text)
    return text


def source_label(source):
    """Label for a single citation or finding source."""
    if source == "technician":
        return "Technician"
    if source == "system":
        return "System"
    return humanize(source)


def sentence(text):
    """Capitalise the first letter only; leave the rest as the technician wrote it."""
    text = (text or "").strip()
    return text[:1].upper() + text[1:]


def register_filters(templates):
    templates.env.filters.update({"humanize": humanize, "source_label": source_label, "sentence": sentence})
