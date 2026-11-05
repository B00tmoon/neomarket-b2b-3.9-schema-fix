"""Idempotency stores for reserve / unreserve operations (B2B-8)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime
from sqlalchemy.dialects.postgresql import JSONB, UUID

from src.models.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ReserveOperation(Base):
    """Successful reserve results keyed by client idempotency_key."""

    __tablename__ = "reserve_operations"

    idempotency_key = Column(UUID(as_uuid=True), primary_key=True)
    order_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    result = Column(JSONB, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class UnreserveOperation(Base):
    """Idempotent unreserve by order_id — second call is a no-op."""

    __tablename__ = "unreserve_operations"

    order_id = Column(UUID(as_uuid=True), primary_key=True)
    result = Column(JSONB, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
