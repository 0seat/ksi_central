"""M5: 캔들 캐시

Revision ID: 0003_candles
Revises: 0002_api_keys_instruments
Create Date: 2026-10-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_candles"
down_revision = "0002_api_keys_instruments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "candles",
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("interval", sa.String(length=4), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.String(length=48), nullable=False),
        sa.Column("high", sa.String(length=48), nullable=False),
        sa.Column("low", sa.String(length=48), nullable=False),
        sa.Column("close", sa.String(length=48), nullable=False),
        sa.Column("volume", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("symbol", "interval", "ts"),
    )
    op.create_table(
        "candle_coverage",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("interval", sa.String(length=4), nullable=False),
        sa.Column("start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("candle_coverage")
    op.drop_table("candles")
