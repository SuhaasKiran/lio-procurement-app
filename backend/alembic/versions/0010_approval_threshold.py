"""support automatic approval below the USD threshold

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-25
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_procurement_request_approval_status",
        "procurement_requests",
        type_="check",
    )
    op.create_check_constraint(
        "ck_procurement_request_approval_status",
        "procurement_requests",
        "approval_status IN ('pending', 'approved', 'rejected', 'not_required')",
    )
    op.add_column(
        "procurement_requests", sa.Column("approval_note", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("procurement_requests", "approval_note")
    op.drop_constraint(
        "ck_procurement_request_approval_status",
        "procurement_requests",
        type_="check",
    )
    op.create_check_constraint(
        "ck_procurement_request_approval_status",
        "procurement_requests",
        "approval_status IN ('pending', 'approved', 'rejected')",
    )
