"""M5: 종목 마스터 필드(kind, name_en, source), 개장일 테이블

Revision ID: 0004_master_calendar
Revises: 0003_candles
Create Date: 2026-10-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_master_calendar"
down_revision = "0003_candles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("instruments") as batch:
        batch.add_column(sa.Column("kind", sa.String(length=8), nullable=False, server_default="stock"))
        batch.add_column(sa.Column("name_en", sa.String(length=80), nullable=False, server_default=""))
        batch.add_column(sa.Column("source", sa.String(length=8), nullable=False, server_default="manual"))
    op.create_table(
        "market_days",
        sa.Column("market", sa.String(length=4), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("open", sa.Boolean(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("market", "day"),
    )


def downgrade() -> None:
    op.drop_table("market_days")
    with op.batch_alter_table("instruments") as batch:
        batch.drop_column("source")
        batch.drop_column("name_en")
        batch.drop_column("kind")
