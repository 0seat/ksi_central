"""과거 시세 보관 대상 앱 등록

Revision ID: 0006_history_subscriptions
Revises: 0005_ops
Create Date: 2026-10-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_history_subscriptions"
down_revision = "0005_ops"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "history_subscriptions",
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("owner", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("symbol", "owner"),
    )


def downgrade() -> None:
    op.drop_table("history_subscriptions")
