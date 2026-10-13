"""원장/홀드/포지션 저수준 연산. 모든 함수는 호출자가 연 세션(트랜잭션) 안에서 동작한다."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from central.core.money import ZERO
from central.storage.models import HoldRow, LedgerRow, PositionRow

ACTIVE = "active"
RELEASED = "released"


def post(
    s: Session,
    sub_account_id: int,
    currency: str,
    type_: str,
    amount: Decimal,
    *,
    at: datetime,
    ref_type: str | None = None,
    ref_id: int | None = None,
    fx_rate: Decimal | None = None,
    provisional: bool = False,
    memo: str | None = None,
) -> LedgerRow:
    row = LedgerRow(
        sub_account_id=sub_account_id, currency=currency, type=type_, amount=amount, fx_rate=fx_rate,
        provisional=provisional, ref_type=ref_type, ref_id=ref_id, memo=memo, created_at=at,
    )
    s.add(row)
    s.flush()
    return row


def cash_balance(s: Session, sub_account_id: int, currency: str) -> Decimal:
    rows = s.scalars(
        select(LedgerRow.amount).where(LedgerRow.sub_account_id == sub_account_id, LedgerRow.currency == currency)
    )
    return sum(rows, ZERO)


def held_cash(s: Session, sub_account_id: int, currency: str, exclude_order_id: int | None = None) -> Decimal:
    q = select(HoldRow).where(
        HoldRow.sub_account_id == sub_account_id, HoldRow.kind == "cash",
        HoldRow.currency == currency, HoldRow.status == ACTIVE,
    )
    return sum((h.remaining for h in s.scalars(q) if h.order_id != exclude_order_id), ZERO)


def available_cash(s: Session, sub_account_id: int, currency: str, exclude_order_id: int | None = None) -> Decimal:
    return cash_balance(s, sub_account_id, currency) - held_cash(s, sub_account_id, currency, exclude_order_id)


def held_qty(s: Session, sub_account_id: int, instrument_id: str, exclude_order_id: int | None = None) -> int:
    q = select(HoldRow).where(
        HoldRow.sub_account_id == sub_account_id, HoldRow.kind == "qty",
        HoldRow.instrument_id == instrument_id, HoldRow.status == ACTIVE,
    )
    return int(sum((h.remaining for h in s.scalars(q) if h.order_id != exclude_order_id), ZERO))


def position(s: Session, sub_account_id: int, instrument_id: str) -> PositionRow | None:
    return s.get(PositionRow, (sub_account_id, instrument_id))


def available_qty(s: Session, sub_account_id: int, instrument_id: str, exclude_order_id: int | None = None) -> int:
    pos = position(s, sub_account_id, instrument_id)
    return (pos.qty if pos else 0) - held_qty(s, sub_account_id, instrument_id, exclude_order_id)


def order_holds(s: Session, order_id: int) -> list[HoldRow]:
    return list(s.scalars(select(HoldRow).where(HoldRow.order_id == order_id, HoldRow.status == ACTIVE)))


def release_holds(s: Session, order_id: int) -> None:
    for h in order_holds(s, order_id):
        h.remaining = ZERO
        h.status = RELEASED
