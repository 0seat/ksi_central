"""M4: 서브계좌 API 키, 종목 테이블

Revision ID: 0002_api_keys_instruments
Revises: 0001_initial
Create Date: 2026-10-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_api_keys_instruments"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sub_account_id", sa.Integer(), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column("label", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["sub_account_id"], ["sub_accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash"),
    )
    op.create_table(
        "instruments",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("market", sa.String(length=4), nullable=False),
        sa.Column("code", sa.String(length=16), nullable=False),
        sa.Column("exchange", sa.String(length=8), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("nxt_tradable", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("instruments")
    op.drop_table("api_keys")
