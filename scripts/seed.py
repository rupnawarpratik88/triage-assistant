"""Load demo reports so a reviewer can try the app immediately.
Safe to run repeatedly: it skips when data already exists.
Usage:  python scripts/seed.py            (skip if data exists)
        python scripts/seed.py --force    (add the demo reports again)
The demo reports go through the real submission route, so rules and retrieval run exactly as for users.
AI analysis is not pre-filled: click "Run AI analysis" on a report."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEMO_REPORTS = [
    {   # critical threshold breach + a warning
        "equipment_type": "pump", "identifier": "P-101",
        "description": "Pump running hot and noisy after restart",
        "recent_events": "Restarted after maintenance yesterday\nLoad increased to 90% this morning",
        "temp_c": "98", "vibration_mm_s": "5.2", "pressure_bar": "4.8",
    },
    {   # conflicting data: description says overheating, sensor reads normal; vibration missing
        "equipment_type": "motor", "identifier": "M-204",
        "description": "Motor is overheating and trips occasionally",
        "recent_events": "Tripped twice on overcurrent last night",
        "temp_c": "60", "current_dev_pct": "12",
    },
    {   # low pressure warning, partial readings
        "equipment_type": "compressor", "identifier": "C-310",
        "description": "Pressure not reaching setpoint",
        "recent_events": "Intake filter last replaced 4 months ago",
        "pressure_bar": "6.2", "oil_pressure_bar": "3.1", "temp_c": "90",
    },
    {   # no sensor readings at all
        "equipment_type": "conveyor", "identifier": "CV-12",
        "description": "Belt slipping under load",
        "recent_events": "Belt tension adjusted last week",
    },
]


def seed(client):
    """Post every demo report through the app. Returns how many were created."""
    for data in DEMO_REPORTS:
        response = client.post("/reports", data=data, follow_redirects=False)
        if response.status_code != 303:
            raise RuntimeError(f"Could not create demo report for {data['identifier']}: {response.status_code}")
    return len(DEMO_REPORTS)


def main():
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select

    from app.db import SessionLocal
    from app.main import app
    from app.models import Equipment

    with SessionLocal() as db:
        existing = db.scalar(select(func.count()).select_from(Equipment))
    if existing and "--force" not in sys.argv:
        print(f"Seed skipped: {existing} equipment record(s) already exist. Use --force to add demo data anyway.")
        return
    count = seed(TestClient(app))
    print(f"Seeded {count} demo reports.")


if __name__ == "__main__":
    main()
