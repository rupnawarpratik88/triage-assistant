import json

import app.ai as ai_mod
import app.main as main_mod
from app.ai import AIError
from app.retrieval import RetrievalError
from tests.test_ai import GOOD
from tests.test_reports import make_client

FORM = {
    "equipment_type": "pump", "identifier": "P-301",
    "description": "high bearing temperature", "recent_events": "Restarted yesterday", "temp_c": "98",
}


def create(client):
    r = client.post("/reports", data=FORM, follow_redirects=False)
    return r.headers["location"].split("?")[0]


def test_analysis_creates_possible_causes_and_draft_work_order(monkeypatch):
    monkeypatch.setattr(ai_mod, "call_llm", lambda system, prompt: json.dumps(GOOD))
    client = make_client()
    url = create(client)
    r = client.post(url + "/analyze", follow_redirects=False)
    assert r.status_code == 303
    page = client.get(url)
    assert "Possible causes" in page.text
    assert "Draft work order" in page.text
    assert "DRAFT" in page.text
    assert "raised by the deterministic threshold rules" in page.text
    api = client.get("/api/reports/1").json()
    assert api["ai_status"] == "ok"
    assert api["priority"] == "high"
    assert [f["kind"] for f in api["findings"]].count("possible_cause") == 1
    assert api["work_orders"][0]["status"] == "draft"


def test_ai_failure_is_shown_and_checks_still_display(monkeypatch):
    def boom(system, prompt):
        raise AIError("The AI service timed out. Try again.")

    monkeypatch.setattr(ai_mod, "call_llm", boom)
    client = make_client()
    url = create(client)
    client.post(url + "/analyze", follow_redirects=False)
    page = client.get(url)
    assert "AI analysis failed" in page.text
    assert "timed out" in page.text
    assert "CRITICAL" in page.text
    assert "Draft work order" not in page.text
    assert "Retry AI analysis" in page.text


def test_analysis_without_retrieval_is_blocked(monkeypatch):
    def boom(*args, **kwargs):
        raise RetrievalError("Knowledge base unavailable")

    monkeypatch.setattr(main_mod, "retrieve", boom)
    client = make_client()
    url = create(client)
    client.post(url + "/analyze", follow_redirects=False)
    api = client.get("/api/reports/1").json()
    assert api["ai_status"] == "failed"
    assert "manual evidence" in api["ai_error"]
    assert api["work_orders"] == []


def test_running_analysis_twice_does_not_duplicate(monkeypatch):
    monkeypatch.setattr(ai_mod, "call_llm", lambda system, prompt: json.dumps(GOOD))
    client = make_client()
    url = create(client)
    client.post(url + "/analyze", follow_redirects=False)
    client.post(url + "/analyze", follow_redirects=False)
    api = client.get("/api/reports/1").json()
    assert [f["kind"] for f in api["findings"]].count("possible_cause") == 1
    assert len(api["work_orders"]) == 1
