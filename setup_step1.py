"""
Run this ONCE inside an empty folder:  python setup_step1.py
It creates the whole Step 1 project (app/, kb/, tests/, scripts/, requirements.txt, .gitignore).
"""
import os

FILES = {}

FILES["requirements.txt"] = '''fastapi
uvicorn
sqlalchemy
pydantic
jinja2
python-multipart
pytest
httpx
rank-bm25
'''

FILES[".gitignore"] = '''.venv/
__pycache__/
*.db
.env
'''

FILES["app/__init__.py"] = ""
FILES["tests/__init__.py"] = ""
FILES["scripts/.gitkeep"] = ""

FILES["app/db.py"] = '''from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

engine = create_engine("sqlite:///triage.db", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
'''

FILES["app/models.py"] = '''from datetime import datetime, timezone
from sqlalchemy import String, Text, ForeignKey, JSON, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .db import Base


def now():
    return datetime.now(timezone.utc)


class Equipment(Base):
    __tablename__ = "equipment"
    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(50))  # pump, motor, compressor, conveyor
    identifier: Mapped[str] = mapped_column(String(50), unique=True)
    reports: Mapped[list["Report"]] = relationship(back_populates="equipment")


class Report(Base):
    __tablename__ = "reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    equipment_id: Mapped[int] = mapped_column(ForeignKey("equipment.id"))
    description: Mapped[str] = mapped_column(Text)
    recent_events: Mapped[str] = mapped_column(Text, default="")
    sensors: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # optional readings
    ai_status: Mapped[str] = mapped_column(String(20), default="pending")  # pending/ok/failed
    ai_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    equipment: Mapped[Equipment] = relationship(back_populates="reports")
    findings: Mapped[list["Finding"]] = relationship(back_populates="report")
    work_orders: Mapped[list["WorkOrder"]] = relationship(back_populates="report")


class Finding(Base):
    """kind: observation | possible_cause | confirmed_finding
    origin: rule | ai | technician. Only a technician may create confirmed_finding."""
    __tablename__ = "findings"
    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("reports.id"))
    kind: Mapped[str] = mapped_column(String(30))
    origin: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(100))  # e.g. "pump:3", "event:1", "sensor:temp_c"
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    report: Mapped[Report] = relationship(back_populates="findings")


class WorkOrder(Base):
    __tablename__ = "work_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("reports.id"))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft/approved/rejected
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    report: Mapped[Report] = relationship(back_populates="work_orders")


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity: Mapped[str] = mapped_column(String(30))
    entity_id: Mapped[int]
    action: Mapped[str] = mapped_column(String(50))
    actor: Mapped[str] = mapped_column(String(30))  # system / ai / technician
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
'''

FILES["app/main.py"] = '''from fastapi import FastAPI
from .db import Base, engine
from . import models  # noqa: F401

Base.metadata.create_all(engine)
app = FastAPI(title="Equipment Maintenance Triage Assistant")


@app.get("/health")
def health():
    return {"status": "ok"}
'''

FILES["kb/pump.md"] = '''# Centrifugal Pump Manual

## 1. Normal operating limits
Bearing temperature normal below 75 C. Warning above 85 C. Vibration normal below 4.5 mm/s RMS. Discharge pressure normal 4-6 bar.

## 2. Fault: high bearing temperature
Possible causes: insufficient lubrication, bearing wear, shaft misalignment, blocked cooling. Inspect: check oil level and condition, listen for abnormal noise, check coupling alignment, verify cooling flow.

## 3. Fault: high vibration
Possible causes: cavitation, impeller imbalance, loose mounting, misalignment. Inspect: check suction pressure, inspect impeller for damage, check foundation bolt torque, check alignment.

## 4. Fault: low discharge pressure
Possible causes: worn impeller, air in suction line, closed or partly blocked valve, seal leakage. Inspect: check suction line for leaks, verify valve positions, inspect mechanical seal.

## 5. Safety
Lock out and tag out before any inspection of rotating parts. Never open the casing while pressurised.
'''

FILES["kb/motor.md"] = '''# Electric Induction Motor Manual

## 1. Normal operating limits
Winding temperature normal below 100 C. Warning above 120 C. Vibration normal below 3.5 mm/s RMS. Current should stay within 10 percent of nameplate rating.

## 2. Fault: overheating
Possible causes: overload, blocked ventilation, voltage imbalance, bearing failure, frequent starts. Inspect: check current on all phases, clean cooling fins and fan cover, check supply voltage, review start frequency.

## 3. Fault: excessive vibration or noise
Possible causes: bearing wear, rotor imbalance, loose base bolts, misaligned coupling. Inspect: listen at bearing housings, check base bolts, check coupling alignment.

## 4. Fault: trips on overcurrent
Possible causes: mechanical jam, low supply voltage, winding insulation damage, overloaded driven equipment. Inspect: rotate shaft by hand when isolated, measure insulation resistance, check driven load.

## 5. Safety
Isolate and lock out the supply before inspection. Wait for the motor to cool before touching windings or housing.
'''

FILES["kb/compressor.md"] = '''# Air Compressor Manual

## 1. Normal operating limits
Discharge air temperature normal below 95 C. Warning above 110 C. Discharge pressure normal 7-8 bar. Oil pressure normal 2-4 bar. Vibration normal below 5.0 mm/s RMS.

## 2. Fault: high discharge temperature
Possible causes: dirty cooler, low oil level, blocked air filter, failed cooling fan, high ambient temperature. Inspect: check oil level, clean cooler fins, inspect intake filter, check fan operation.

## 3. Fault: pressure not reaching setpoint
Possible causes: air leaks in piping, worn inlet valve, clogged intake filter, faulty pressure switch. Inspect: do a leak check on joints and hoses, inspect inlet valve, replace filter if dirty, test pressure switch.

## 4. Fault: low oil pressure
Possible causes: low oil level, clogged oil filter, worn oil pump, wrong oil grade. Inspect: check oil level and grade, replace oil filter, inspect oil pump drive.

## 5. Safety
Depressurise the receiver and lock out power before opening any part. Hot surfaces can cause burns after shutdown.
'''

FILES["kb/conveyor.md"] = '''# Belt Conveyor Manual

## 1. Normal operating limits
Drive motor current within 10 percent of rating. Gearbox temperature normal below 80 C. Warning above 95 C. Belt speed deviation should stay below 5 percent. Roller bearing temperature normal below 70 C.

## 2. Fault: belt slipping
Possible causes: low belt tension, worn lagging on drive pulley, overload, wet or oily belt surface. Inspect: check take-up tension, inspect pulley lagging, check load on belt, clean belt surface.

## 3. Fault: belt mistracking
Possible causes: misaligned idlers, uneven loading, material build-up on rollers, bent frame. Inspect: check idler alignment, check loading point, clean rollers, inspect frame for damage.

## 4. Fault: gearbox overheating
Possible causes: low oil level, wrong oil grade, overload, blocked breather. Inspect: check oil level and grade, check breather, check motor current, check for abnormal noise.

## 5. Safety
Lock out and tag out the drive before inspection. Never reach near moving belts or rollers. Keep guards in place.
'''

README_NOTE = '''# Equipment Maintenance Triage Assistant

(README will be completed in the final step.)
'''
FILES["README.md"] = README_NOTE


def main():
    for path, content in FILES.items():
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        if os.path.exists(path):
            print(f"skip (exists): {path}")
            continue
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"created: {path}")
    print("\nDone. Next:")
    print("  python -m venv .venv")
    print("  .venv\\Scripts\\activate     (Windows)   |   source .venv/bin/activate   (Mac/Linux)")
    print("  pip install -r requirements.txt")
    print("  uvicorn app.main:app --reload")
    print("  open http://127.0.0.1:8000/health")


if __name__ == "__main__":
    main()
