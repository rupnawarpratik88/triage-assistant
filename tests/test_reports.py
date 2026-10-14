from fastapi.testclient import TestClient
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
