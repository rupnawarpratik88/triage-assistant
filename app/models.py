from datetime import datetime, timezone
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
    severity: Mapped[str | None] = mapped_column(String(20), nullable=True)  # critical/warning/ok/data_quality/info
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
