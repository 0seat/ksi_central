"""대시보드 레이아웃

Revision ID: 0007_ui_layouts
Revises: 0006_history_subscriptions
Create Date: 2026-10-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_ui_layouts"
down_revision = "0006_history_subscriptions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ui_layouts",
        sa.Column("owner", sa.String(length=32), nullable=False),
        sa.Column("layout", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("owner"),
    )


def downgrade() -> None:
    op.drop_table("ui_layouts")
