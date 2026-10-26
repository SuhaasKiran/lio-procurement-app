"""store quote adjustments and reconciliation notes

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-25
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "procurement_requests",
        sa.Column("financial_breakdown", sa.JSON(), nullable=True),
    )
    op.add_column(
        "procurement_requests",
        sa.Column("discrepancy_note", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("procurement_requests", "discrepancy_note")
    op.drop_column("procurement_requests", "financial_breakdown")
