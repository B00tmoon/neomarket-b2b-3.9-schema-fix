"""Add order_id to reserve_operations (align model ↔ schema).

Revision ID: 002
Revises: 001
Create Date: 2026-11-03

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent for DBs that already got the column via a fixed 001
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("reserve_operations")}
    if "order_id" not in cols:
        op.add_column(
            "reserve_operations",
            sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
        op.create_index(
            "ix_reserve_operations_order_id",
            "reserve_operations",
            ["order_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("reserve_operations")}
    if "order_id" in cols:
        op.drop_index(
            "ix_reserve_operations_order_id",
            table_name="reserve_operations",
        )
        op.drop_column("reserve_operations", "order_id")
