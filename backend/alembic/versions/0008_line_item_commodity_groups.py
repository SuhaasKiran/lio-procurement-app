"""assign commodity groups to individual order lines

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-25
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "order_lines",
        sa.Column("commodity_group_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "order_lines",
        sa.Column(
            "classification_source",
            sa.String(length=16),
            server_default="legacy",
            nullable=False,
        ),
    )
    op.create_foreign_key(
        "fk_order_lines_commodity_group_id",
        "order_lines",
        "commodity_groups",
        ["commodity_group_id"],
        ["id"],
    )
    op.create_index(
        "ix_order_lines_commodity_group_id", "order_lines", ["commodity_group_id"]
    )
    op.create_check_constraint(
        "ck_order_line_classification_source",
        "order_lines",
        "classification_source IN ('llm', 'manual', 'legacy')",
    )
    op.execute(
        sa.text(
            "UPDATE order_lines SET commodity_group_id = procurement_requests.commodity_group_id "
            "FROM procurement_requests WHERE order_lines.request_id = procurement_requests.id"
        )
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_order_line_classification_source", "order_lines", type_="check"
    )
    op.drop_index("ix_order_lines_commodity_group_id", table_name="order_lines")
    op.drop_constraint(
        "fk_order_lines_commodity_group_id", "order_lines", type_="foreignkey"
    )
    op.drop_column("order_lines", "classification_source")
    op.drop_column("order_lines", "commodity_group_id")
