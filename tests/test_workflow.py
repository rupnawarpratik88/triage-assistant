import json

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
