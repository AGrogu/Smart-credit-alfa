"""Versioned JSON aggregates; independent of the preserved first prototype tables."""

from sqlalchemy import String, Integer, JSON, LargeBinary, DateTime, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base
from .models import now


class CreditRecord(Base):
    __tablename__ = "credit_v2_records"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    kind: Mapped[str] = mapped_column(String(30), index=True)
    organization_id: Mapped[str | None] = mapped_column(String(100), index=True, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (Index("ix_credit_v2_org_kind", "organization_id", "kind"),)


class CreditBlob(Base):
    __tablename__ = "credit_v2_blobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("credit_v2_records.id"), index=True)
    content: Mapped[bytes] = mapped_column(LargeBinary)
