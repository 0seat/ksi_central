"""주문 전 리스크 점검과 긴급 정지(kill switch).

한도는 서브계좌 x 시장 단위이며 금액은 해당 시장 통화 기준이다.
일일 손실 한도에 도달하면 신규 매수만 막고 매도(포지션 축소)는 허용한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from central.core.instruments import Instrument
from central.core.money import ZERO
from central.oms import ledger
from central.oms.errors import OmsError
from central.storage.db import Database
from central.storage.models import FillRow, OrderRow, RiskLimitRow

OPEN_STATUSES = ("SUBMITTING", "ACCEPTED", "PARTIALLY_FILLED", "CANCEL_PENDING", "UNKNOWN")


@dataclass(frozen=True)
class RiskLimits:
    max_order_amount: Decimal | None = None
    max_position_amount: Decimal | None = None
    max_daily_loss: Decimal | None = None
    max_open_orders: int | None = None
    allowed_symbols: list[str] | None = None


@dataclass
class RiskEngine:
    db: Database
    # ("all",""), ("broker",id), ("broker_market","id:KR"), ("sub",id), ("market",KR)
    halted: set[tuple[str, str]] = field(default_factory=set)

    # ───────────── 설정 ─────────────
    def set_limits(self, sub_account_id: int, market: str, limits: RiskLimits) -> None:
        with self.db.session() as s, s.begin():
            row = s.scalar(select(RiskLimitRow).where(
                RiskLimitRow.sub_account_id == sub_account_id, RiskLimitRow.market == market))
            if row is None:
                row = RiskLimitRow(sub_account_id=sub_account_id, market=market)
                s.add(row)
            row.max_order_amount = limits.max_order_amount
            row.max_position_amount = limits.max_position_amount
            row.max_daily_loss = limits.max_daily_loss
            row.max_open_orders = limits.max_open_orders
            row.allowed_symbols = limits.allowed_symbols

    def halt(self, scope: str, id_: str = "") -> None:
        self.halted.add((scope, id_))

    def resume(self, scope: str, id_: str = "") -> None:
        self.halted.discard((scope, id_))

    def is_halted(self, broker_account_id: str, sub_account_id: int, market: str) -> bool:
        scopes = {("all", ""), ("broker", broker_account_id), ("broker_market", f"{broker_account_id}:{market}"),
                  ("sub", str(sub_account_id)), ("market", market)}
        return bool(scopes & self.halted)

    # ───────────── 점검 ─────────────
    def check(
        self, s: Session, *, broker_account_id: str, sub_account_id: int, instrument: Instrument,
        side: str, qty: int, est_price: Decimal, now: datetime,
    ) -> None:
        if self.is_halted(broker_account_id, sub_account_id, instrument.market):
            raise OmsError("HALTED", "긴급 정지 상태")
        row = s.scalar(select(RiskLimitRow).where(
            RiskLimitRow.sub_account_id == sub_account_id, RiskLimitRow.market == instrument.market))
        if row is None:
            return
        notional = est_price * qty

        if row.allowed_symbols is not None and instrument.id not in row.allowed_symbols:
            raise OmsError("RISK_LIMIT_EXCEEDED", f"허용 종목 아님: {instrument.id}")
        if row.max_open_orders is not None:
            open_count = s.scalar(select(func.count()).select_from(OrderRow).where(
                OrderRow.sub_account_id == sub_account_id, OrderRow.status.in_(OPEN_STATUSES))) or 0
            if open_count >= row.max_open_orders:
                raise OmsError("RISK_LIMIT_EXCEEDED", f"미체결 주문 수 한도({row.max_open_orders})")
        if row.max_order_amount is not None and notional > row.max_order_amount:
            raise OmsError("RISK_LIMIT_EXCEEDED", f"1회 주문금액 한도({row.max_order_amount}) 초과")
        if side == "buy":
            if row.max_position_amount is not None:
                pos = ledger.position(s, sub_account_id, instrument.id)
                current = (pos.avg_price * pos.qty) if pos else ZERO
                if current + notional > row.max_position_amount:
                    raise OmsError("RISK_LIMIT_EXCEEDED", f"종목 보유금액 한도({row.max_position_amount}) 초과")
            if row.max_daily_loss is not None:
                loss = -self.realized_today(s, sub_account_id, instrument.currency, now)
                if loss >= row.max_daily_loss:
                    raise OmsError("RISK_LIMIT_EXCEEDED", f"일일 손실 한도({row.max_daily_loss}) 도달, 신규 매수 차단")

    @staticmethod
    def realized_today(s: Session, sub_account_id: int, currency: str, now: datetime) -> Decimal:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        rows = s.scalars(select(FillRow.realized_pnl).where(
            FillRow.sub_account_id == sub_account_id, FillRow.currency == currency, FillRow.filled_at >= start))
        return sum(rows, ZERO)
