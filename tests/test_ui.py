import json

import app.ai as ai_mod
from app.labels import humanize, sentence, source_label
from tests.test_ai import GOOD
from tests.test_reports import make_client

FORM = {
    "equipment_type": "pump", "identifier": "P-910", "description": "high bearing temperature",
    "recent_events": "restarted after maintenance yesterday", "temp_c": "98",
}


def made(client):
    r = client.post("/reports", data=FORM, follow_redirects=False)
    return r.headers["location"].split("?")[0]


def test_label_helpers():
    assert humanize("(manual pump:1)") == "(Pump manual, section 1)"
    assert source_label("event:2") == "Event 2"
    assert source_label("report:description") == "Issue description"
    assert source_label("sensor:vibration_mm_s") == "Vibration sensor (mm/s)"
    assert source_label("technician") == "Technician"
    assert sentence("restarted after maintenance") == "Restarted after maintenance"


def test_disclaimer_is_pinned_in_a_sticky_top_bar():
    page = make_client().get("/").text
    assert 'class="topbar"' in page
    assert "position: sticky" in page
    assert "DECISION SUPPORT ONLY" in page


def test_report_page_uses_readable_labels_and_badges():
    client = make_client()
    page = client.get(made(client)).text
    assert "Vibration sensor (mm/s)" in page
    assert "Pump manual, section 1" in page
    assert "(manual pump:1)" not in page
    assert "Event 1:" in page and "Restarted after maintenance yesterday" in page
    assert "(score" not in page and "Relevance:" in page
    assert "sev-critical" in page and "CRITICAL" in page
    assert "DATA GAP" in page


def test_action_bar_before_and_after_analysis(monkeypatch):
    monkeypatch.setattr(ai_mod, "call_llm", lambda system, prompt: json.dumps(GOOD))
    client = make_client()
    url = made(client)
    before = client.get(url).text
    assert "Next steps" in before and "Run AI analysis" in before
    assert "/work-orders/1/approve" not in before
    client.post(url + "/analyze", follow_redirects=False)
    after = client.get(url).text
    assert "Approve work order" in after and "/work-orders/1/approve" in after
    assert "Reject draft" in after and "Review &amp; edit work order" in after
    client.post("/work-orders/1/approve", follow_redirects=False)
    final = client.get(url).text
    assert "View work order (APPROVED)" in final
    assert "/work-orders/1/approve" not in final


def test_work_order_citations_are_readable(monkeypatch):
    monkeypatch.setattr(ai_mod, "call_llm", lambda system, prompt: json.dumps(GOOD))
    client = make_client()
    url = made(client)
    client.post(url + "/analyze", follow_redirects=False)
    body = client.get("/api/reports/1").json()["work_orders"][0]["body"]
    assert "Pump manual, section 2" in body
    assert "[pump:2" not in body


def test_sensor_issue_fallback_is_recorded_once():
    client = make_client()
    url = made(client)
    data = {"source": "sensor:vibration_mm_s"}
    r = client.post(url + "/sensor-issue", data=data, follow_redirects=False)
    assert r.status_code == 303 and "notice=sensor_reported" in r.headers["location"]
    client.post(url + "/sensor-issue", data=data, follow_redirects=False)
    findings = client.get("/api/reports/1").json()["findings"]
    reported = [f for f in findings if f["origin"] == "technician" and f["kind"] == "observation"]
    assert len(reported) == 1
    page = client.get(url).text
    assert "Technician reported a sensor issue" in page and "REPORTED" in page
    assert "Sensor issue already reported" in page


def test_sensor_issue_requires_a_flagged_sensor():
    client = make_client()
    url = made(client)
    assert client.post(url + "/sensor-issue", data={"source": "sensor:temp_c"}, follow_redirects=False).status_code == 400
    assert client.post(url + "/sensor-issue", data={"source": "nonsense"}, follow_redirects=False).status_code == 400
    assert client.post("/reports/999/sensor-issue", data={"source": "sensor:temp_c"}, follow_redirects=False).status_code == 404
