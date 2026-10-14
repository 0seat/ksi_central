"""M6: 긴급 정지 상태 영속화, 운영 스케줄러 실행 기록

Revision ID: 0005_ops
Revises: 0004_master_calendar
Create Date: 2026-10-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_ops"
down_revision = "0004_master_calendar"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "halts",
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("scope", "id"),
    )
    op.create_table(
        "job_runs",
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("run_date", sa.Date(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("detail", sa.String(length=2000), nullable=False),
        sa.PrimaryKeyConstraint("name"),
    )


def downgrade() -> None:
    op.drop_table("job_runs")
    op.drop_table("halts")
