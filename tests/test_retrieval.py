import pytest

import app.main as main_mod
from app.retrieval import RetrievalError, load_chunks, retrieve
from tests.test_reports import make_client


def ids(hits):
    return [h["id"] for h in hits]


def test_chunk_ids_are_parsed_from_kb():
    found = {c["id"] for c in load_chunks()}
    assert "pump:2" in found and "motor:1" in found and "conveyor:4" in found


def test_bearing_temperature_query_finds_fault_section():
    hits = retrieve("pump", "high bearing temperature")
    assert "pump:2" in ids(hits)


def test_vibration_query_finds_vibration_section():
    hits = retrieve("pump", "strong vibration and shaking")
    assert "pump:3" in ids(hits)


def test_results_only_come_from_the_matching_manual():
    hits = retrieve("motor", "overheating winding")
    assert all(i.startswith("motor:") for i in ids(hits))
    assert "motor:2" in ids(hits)


def test_safety_section_is_always_included():
    hits = retrieve("pump", "high bearing temperature")
    assert "pump:5" in ids(hits)


def test_nonsense_query_raises_retrieval_error():
    with pytest.raises(RetrievalError):
        retrieve("pump", "zzzz qqqq")


def test_empty_kb_raises_retrieval_error(tmp_path):
    with pytest.raises(RetrievalError):
        retrieve("pump", "high temperature", kb_dir=tmp_path)


def test_report_page_shows_retrieved_sections():
    client = make_client()
    r = client.post("/reports", data={
        "equipment_type": "pump", "identifier": "P-201",
        "description": "high bearing temperature", "temp_c": "98",
    }, follow_redirects=False)
    page = client.get(r.headers["location"])
    assert "pump:2" in page.text


def test_retrieval_failure_is_shown_but_threshold_checks_still_display(monkeypatch):
    def boom(*args, **kwargs):
        raise RetrievalError("Knowledge base unavailable")

    monkeypatch.setattr(main_mod, "retrieve", boom)
    client = make_client()
    r = client.post("/reports", data={
        "equipment_type": "pump", "identifier": "P-202",
        "description": "pump running hot", "temp_c": "98",
    }, follow_redirects=False)
    page = client.get(r.headers["location"])
    assert "Retrieval failed" in page.text
    assert "Knowledge base unavailable" in page.text
    assert "CRITICAL" in page.text
