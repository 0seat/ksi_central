"""조회 전용 쿼리 (API 의 원장, 주문, 체결, 손익 목록).

SQLite 는 시각을 시간대 없이 저장하므로 저장값은 KST 로 간주한다.
조건으로 받은 시각은 KST 로 변환해 비교하고, 결과 시각에는 KST 를 붙인다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import select

from central.core.market_hours import KST
from central.core.money import ZERO
from central.oms.orders import OrderView, _view
from central.risk.engine import RiskLimits
from central.storage.db import Database
from central.storage.models import FeeScheduleRow, FillRow, LedgerRow, OrderRow, ReconciliationRow, RiskLimitRow

MAX_LIMIT = 1000


def kst(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=KST) if dt.tzinfo is None else dt.astimezone(KST)


def day_start(d: date) -> datetime:
    return datetime.combine(d, time.min, KST)


def day_end(d: date) -> datetime:
    return datetime.combine(d, time.max, KST)


def _range(q, column, start: datetime | None, end: datetime | None):
    if start is not None:
        q = q.where(column >= kst(start))
    if end is not None:
        q = q.where(column <= kst(end))
    return q


def _limit(n: int) -> int:
    return max(1, min(n, MAX_LIMIT))


@dataclass(frozen=True)
class LedgerEntry:
    id: int
    currency: str
    type: str
    amount: Decimal
    fx_rate: Decimal | None
    provisional: bool
    ref_type: str | None
    ref_id: int | None
    memo: str | None
    created_at: datetime


@dataclass(frozen=True)
class FillView:
    id: int
    order_id: int | None
    symbol: str
    side: str
    qty: int
    price: Decimal
    fee: Decimal
    tax: Decimal
    realized_pnl: Decimal
    currency: str
    venue: str | None
    filled_at: datetime


def ledger_entries(db: Database, sub_id: int, *, currency: str | None = None, type_: str | None = None,
                   start: datetime | None = None, end: datetime | None = None, limit: int = 200) -> list[LedgerEntry]:
    with db.session() as s:
        q = select(LedgerRow).where(LedgerRow.sub_account_id == sub_id)
        if currency:
            q = q.where(LedgerRow.currency == currency)
        if type_:
            q = q.where(LedgerRow.type == type_)
        q = _range(q, LedgerRow.created_at, start, end).order_by(LedgerRow.id.desc()).limit(_limit(limit))
        return [LedgerEntry(r.id, r.currency, r.type, r.amount, r.fx_rate, r.provisional, r.ref_type, r.ref_id,
                            r.memo, kst(r.created_at)) for r in s.scalars(q)]  # type: ignore[arg-type]


def orders(db: Database, sub_id: int, *, status: str | None = None, symbol: str | None = None,
           market: str | None = None, start: datetime | None = None, end: datetime | None = None,
           limit: int = 200) -> list[OrderView]:
    with db.session() as s:
        q = select(OrderRow).where(OrderRow.sub_account_id == sub_id)
        if status:
            q = q.where(OrderRow.status.in_(status.upper().split(",")))
        if symbol:
            q = q.where(OrderRow.instrument_id == symbol.upper())
        if market:
            q = q.where(OrderRow.market == market.upper())
        q = _range(q, OrderRow.created_at, start, end).order_by(OrderRow.id.desc()).limit(_limit(limit))
        return [_view(o) for o in s.scalars(q)]


def fills(db: Database, sub_id: int, *, symbol: str | None = None, order_id: int | None = None,
          start: datetime | None = None, end: datetime | None = None, limit: int = 200) -> list[FillView]:
    with db.session() as s:
        q = select(FillRow).where(FillRow.sub_account_id == sub_id)
        if symbol:
            q = q.where(FillRow.instrument_id == symbol.upper())
        if order_id is not None:
            q = q.where(FillRow.order_id == order_id)
        q = _range(q, FillRow.filled_at, start, end).order_by(FillRow.id.desc()).limit(_limit(limit))
        return [FillView(f.id, f.order_id, f.instrument_id, f.side, f.qty, f.price, f.fee, f.tax, f.realized_pnl,
                         f.currency, f.venue, kst(f.filled_at)) for f in s.scalars(q)]  # type: ignore[arg-type]


def pnl(db: Database, sub_id: int, *, market: str | None = None,
        start: datetime | None = None, end: datetime | None = None) -> dict[str, dict[str, Decimal | int]]:
    """체결 기준 실현손익 (통화별). realized_pnl 은 수수료와 세금을 뺀 값."""
    with db.session() as s:
        q = select(FillRow).where(FillRow.sub_account_id == sub_id)
        if market:
            q = q.where(FillRow.instrument_id.startswith(f"{market.upper()}:"))
        rows = list(s.scalars(_range(q, FillRow.filled_at, start, end)))
    out: dict[str, dict[str, Decimal | int]] = {}
    for f in rows:
        acc = out.setdefault(f.currency, {"realized_pnl": ZERO, "fee": ZERO, "tax": ZERO,
                                          "buy_amount": ZERO, "sell_amount": ZERO, "fills": 0})
        acc["realized_pnl"] += f.realized_pnl
        acc["fee"] += f.fee
        acc["tax"] += f.tax
        acc["buy_amount" if f.side == "buy" else "sell_amount"] += f.price * f.qty
        acc["fills"] += 1
    return out


def risk_limits(db: Database, sub_id: int) -> dict[str, RiskLimits]:
    with db.session() as s:
        rows = s.scalars(select(RiskLimitRow).where(RiskLimitRow.sub_account_id == sub_id))
        return {r.market: RiskLimits(r.max_order_amount, r.max_position_amount, r.max_daily_loss,
                                     r.max_open_orders, r.allowed_symbols) for r in rows}


def fee_schedules(db: Database, broker_account_id: str | None = None) -> list[dict]:
    with db.session() as s:
        q = select(FeeScheduleRow).order_by(FeeScheduleRow.id)
        if broker_account_id:
            q = q.where(FeeScheduleRow.broker_account_id == broker_account_id)
        return [{"id": r.id, "broker_account_id": r.broker_account_id, "market": r.market, "venue": r.venue,
                 "side": r.side, "fee_rate": r.fee_rate, "min_fee": r.min_fee, "tax_rate": r.tax_rate,
                 "effective_from": r.effective_from} for r in s.scalars(q)]


def reconciliations(db: Database, broker_account_id: str | None = None, limit: int = 50) -> list[dict]:
    with db.session() as s:
        q = select(ReconciliationRow).order_by(ReconciliationRow.id.desc()).limit(_limit(limit))
        if broker_account_id:
            q = q.where(ReconciliationRow.broker_account_id == broker_account_id)
        return [{"id": r.id, "broker_account_id": r.broker_account_id, "market": r.market,
                 "run_at": kst(r.run_at), "ok": r.ok, "diffs": r.diffs} for r in s.scalars(q)]
