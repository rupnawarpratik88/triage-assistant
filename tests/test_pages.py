import json

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
