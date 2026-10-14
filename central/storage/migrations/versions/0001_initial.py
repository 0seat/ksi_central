"""초기 스키마 (M2/M3: 서브계좌, 원장, 홀드, 포지션, 주문, 체결, 수수료, 대사, 이벤트)

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def _dec() -> sa.String:
    # central.storage.db.DecimalText 의 저장 형식
    return sa.String(length=48)


def upgrade() -> None:
    op.create_table(
        "broker_accounts",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("env", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("sub_account_id", sa.Integer(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "fee_schedules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("broker_account_id", sa.String(length=32), nullable=False),
        sa.Column("market", sa.String(length=4), nullable=False),
        sa.Column("venue", sa.String(length=8), nullable=True),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("fee_rate", _dec(), nullable=False),
        sa.Column("min_fee", _dec(), nullable=False),
        sa.Column("tax_rate", _dec(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.ForeignKeyConstraint(["broker_account_id"], ["broker_accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "reconciliations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("broker_account_id", sa.String(length=32), nullable=False),
        sa.Column("market", sa.String(length=4), nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("diffs", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["broker_account_id"], ["broker_accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "sub_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("broker_account_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("markets", sa.JSON(), nullable=False),
        sa.Column("fx_mode", sa.String(length=8), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["broker_account_id"], ["broker_accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("broker_account_id", "name"),
    )
    op.create_table(
        "cash_ledger",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sub_account_id", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("amount", _dec(), nullable=False),
        sa.Column("fx_rate", _dec(), nullable=True),
        sa.Column("provisional", sa.Boolean(), nullable=False),
        sa.Column("ref_type", sa.String(length=16), nullable=True),
        sa.Column("ref_id", sa.Integer(), nullable=True),
        sa.Column("memo", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["sub_account_id"], ["sub_accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ledger_sub_cur", "cash_ledger", ["sub_account_id", "currency"])
    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sub_account_id", sa.Integer(), nullable=False),
        sa.Column("broker_account_id", sa.String(length=32), nullable=False),
        sa.Column("client_order_id", sa.String(length=64), nullable=False),
        sa.Column("broker_order_no", sa.String(length=32), nullable=True),
        sa.Column("broker_org_no", sa.String(length=16), nullable=True),
        sa.Column("orig_order_id", sa.Integer(), nullable=True),
        sa.Column("instrument_id", sa.String(length=32), nullable=False),
        sa.Column("market", sa.String(length=4), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("order_type", sa.String(length=8), nullable=False),
        sa.Column("route", sa.String(length=8), nullable=True),
        sa.Column("price", _dec(), nullable=True),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("filled_qty", sa.Integer(), nullable=False),
        sa.Column("avg_fill_price", _dec(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("reject_reason", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["broker_account_id"], ["broker_accounts.id"]),
        sa.ForeignKeyConstraint(["orig_order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["sub_account_id"], ["sub_accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sub_account_id", "client_order_id"),
    )
    op.create_index("ix_orders_broker_no", "orders", ["broker_account_id", "broker_order_no"])
    op.create_index("ix_orders_sub_status", "orders", ["sub_account_id", "status"])
    op.create_table(
        "positions",
        sa.Column("sub_account_id", sa.Integer(), nullable=False),
        sa.Column("instrument_id", sa.String(length=32), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("avg_price", _dec(), nullable=False),
        sa.Column("realized_pnl", _dec(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["sub_account_id"], ["sub_accounts.id"]),
        sa.PrimaryKeyConstraint("sub_account_id", "instrument_id"),
    )
    op.create_table(
        "risk_limits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sub_account_id", sa.Integer(), nullable=False),
        sa.Column("market", sa.String(length=4), nullable=False),
        sa.Column("max_order_amount", _dec(), nullable=True),
        sa.Column("max_position_amount", _dec(), nullable=True),
        sa.Column("max_daily_loss", _dec(), nullable=True),
        sa.Column("max_open_orders", sa.Integer(), nullable=True),
        sa.Column("allowed_symbols", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["sub_account_id"], ["sub_accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sub_account_id", "market"),
    )
    op.create_table(
        "fills",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("broker_account_id", sa.String(length=32), nullable=False),
        sa.Column("broker_fill_key", sa.String(length=64), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=True),
        sa.Column("sub_account_id", sa.Integer(), nullable=False),
        sa.Column("instrument_id", sa.String(length=32), nullable=False),
        sa.Column("venue", sa.String(length=8), nullable=True),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("price", _dec(), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("fee", _dec(), nullable=False),
        sa.Column("tax", _dec(), nullable=False),
        sa.Column("realized_pnl", _dec(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["broker_account_id"], ["broker_accounts.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["sub_account_id"], ["sub_accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("broker_account_id", "broker_fill_key"),
    )
    op.create_table(
        "holds",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sub_account_id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("instrument_id", sa.String(length=32), nullable=True),
        sa.Column("original", _dec(), nullable=False),
        sa.Column("remaining", _dec(), nullable=False),
        sa.Column("fx_rate", _dec(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["sub_account_id"], ["sub_accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_holds_sub_status", "holds", ["sub_account_id", "status"])


def downgrade() -> None:
    for table in ("holds", "fills", "risk_limits", "positions", "orders", "cash_ledger", "sub_accounts",
                  "reconciliations", "fee_schedules", "events", "broker_accounts"):
        op.drop_table(table)
