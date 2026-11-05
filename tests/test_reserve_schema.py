"""Ensure ReserveOperation model columns match migration-created schema."""

from __future__ import annotations

from pathlib import Path

from src.models.inventory import ReserveOperation, UnreserveOperation


def test_reserve_operations_model_has_order_id():
    cols = {c.name for c in ReserveOperation.__table__.columns}
    assert "idempotency_key" in cols
    assert "order_id" in cols
    assert "result" in cols
    assert "created_at" in cols


def test_unreserve_operations_model_columns():
    cols = {c.name for c in UnreserveOperation.__table__.columns}
    assert "order_id" in cols
    assert "result" in cols
    assert "created_at" in cols


def test_migration_001_defines_order_id_on_reserve_operations():
    text = Path("migrations/versions/001_initial_b2b_schema.py").read_text()
    start = text.index('"reserve_operations"')
    block = text[start : start + 600]
    assert "order_id" in block, "001 migration must create order_id on reserve_operations"


def test_migration_002_aligns_order_id():
    path = Path("migrations/versions/002_reserve_operations_order_id.py")
    assert path.exists()
    text = path.read_text()
    assert "order_id" in text
    assert 'down_revision = "001"' in text
