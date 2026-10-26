"""retain catalog selections on procurement requests

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-25
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "procurement_requests",
        sa.Column("supplier_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "procurement_requests",
        sa.Column("currency", sa.String(length=3), server_default="USD", nullable=False),
    )
    op.create_foreign_key(
        "fk_procurement_requests_supplier_id",
        "procurement_requests",
        "suppliers",
        ["supplier_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_procurement_requests_supplier_id", "procurement_requests", ["supplier_id"]
    )

    op.add_column(
        "order_lines",
        sa.Column("catalog_article_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "order_lines",
        sa.Column("catalog_article_number", sa.Text(), nullable=True),
    )
    op.create_foreign_key(
        "fk_order_lines_catalog_article_id",
        "order_lines",
        "articles",
        ["catalog_article_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_order_lines_catalog_article_id", "order_lines", ["catalog_article_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_order_lines_catalog_article_id", table_name="order_lines")
    op.drop_constraint("fk_order_lines_catalog_article_id", "order_lines", type_="foreignkey")
    op.drop_column("order_lines", "catalog_article_number")
    op.drop_column("order_lines", "catalog_article_id")

    op.drop_index("ix_procurement_requests_supplier_id", table_name="procurement_requests")
    op.drop_constraint(
        "fk_procurement_requests_supplier_id", "procurement_requests", type_="foreignkey"
    )
    op.drop_column("procurement_requests", "currency")
    op.drop_column("procurement_requests", "supplier_id")
