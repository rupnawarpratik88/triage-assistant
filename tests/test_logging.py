import json
import logging

import app.ai as ai_mod
from app.ai import AIError
from app.logging_setup import JsonFormatter
from tests.test_ai import GOOD
from tests.test_reports import make_client

FORM = {
    "equipment_type": "pump", "identifier": "P-700",
    "description": "high bearing temperature", "recent_events": "Restarted yesterday", "temp_c": "98",
}


def events(caplog):
    return {getattr(r, "event", None) for r in caplog.records}


def test_json_formatter_outputs_structured_fields():
    logger = logging.getLogger("t")
    record = logger.makeRecord("t", logging.INFO, "f", 1, "hello", (), None, extra={"event": "x", "report_id": 5})
    data = json.loads(JsonFormatter().format(record))
    assert data["event"] == "x" and data["report_id"] == 5
    assert data["level"] == "INFO" and "ts" in data


def test_report_and_ai_workflow_events_are_logged(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(ai_mod, "call_llm", lambda system, prompt: json.dumps(GOOD))
    client = make_client()
    client.post("/reports", data=FORM, follow_redirects=False)
    client.post("/reports/1/analyze", follow_redirects=False)
    found = events(caplog)
    assert {"report_created", "retrieval_ok", "ai_analysis_started", "ai_output_accepted", "ai_analysis_ok"} <= found


def test_ai_failure_is_logged_as_warning(monkeypatch, caplog):
    caplog.set_level(logging.INFO)

    def boom(system, prompt):
        raise AIError("The AI service timed out. Try again.")

    monkeypatch.setattr(ai_mod, "call_llm", boom)
    client = make_client()
    client.post("/reports", data=FORM, follow_redirects=False)
    client.post("/reports/1/analyze", follow_redirects=False)
    failed = [r for r in caplog.records if getattr(r, "event", "") == "ai_analysis_failed"]
    assert failed and failed[0].levelno == logging.WARNING
    assert "timed out" in failed[0].error


def test_technician_decisions_are_logged(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(ai_mod, "call_llm", lambda system, prompt: json.dumps(GOOD))
    client = make_client()
    client.post("/reports", data=FORM, follow_redirects=False)
    client.post("/reports/1/analyze", follow_redirects=False)
    client.post("/work-orders/1/approve", data={"note": "ok"}, follow_redirects=False)
    assert "work_order_approved" in events(caplog)
