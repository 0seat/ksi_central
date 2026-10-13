"""서브계좌 관리와 자금 이동 (관리자 기능의 핵심 로직)."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Callable, Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from central.core.events import Event, EventBus
from central.core.market_hours import now_kst
from central.core.money import CURRENCIES, ZERO, q_cash
from central.oms import ledger
from central.oms.errors import OmsError
from central.storage.db import Database
from central.storage.models import BrokerAccountRow, EventRow, LedgerRow, PositionRow, SubAccountRow

UNALLOCATED = "unallocated"
SUB_TYPES = ("strategy", "app")
SUB_STATUSES = ("active", "suspended", "closed")
MARKETS = ("KR", "US")
FX_MODES = ("none", "broker")


@dataclass(frozen=True)
class SubAccount:
    id: int
    broker_account_id: str
    name: str
    type: str
    status: str
    markets: tuple[str, ...]
    fx_mode: str


@dataclass(frozen=True)
class CashBalance:
    currency: str
    cash: Decimal
    held: Decimal

    @property
    def available(self) -> Decimal:
        return self.cash - self.held


@dataclass(frozen=True)
class Position:
    instrument_id: str
    currency: str
    qty: int
    sellable: int
    avg_price: Decimal
    realized_pnl: Decimal


def _view(row: SubAccountRow) -> SubAccount:
    return SubAccount(row.id, row.broker_account_id, row.name, row.type, row.status, tuple(row.markets), row.fx_mode)


@contextmanager
def tx(db: Database, bus: EventBus) -> Iterator[tuple[Session, list[Event]]]:
    """트랜잭션 + 이벤트. 커밋이 성공한 뒤에만 이벤트를 발행한다."""
    events: list[Event] = []
    with db.session() as s:
        with s.begin():
            yield s, events
        for e in events:
            bus.publish(e)


def record_event(s: Session, events: list[Event], type_: str, sub_account_id: int | None, payload: dict, at: datetime) -> None:
    s.add(EventRow(ts=at, type=type_, sub_account_id=sub_account_id, payload=payload))
    events.append(Event(type_, sub_account_id, payload, at))


class AccountService:
    def __init__(self, db: Database, bus: EventBus, clock: Callable[[], datetime] = now_kst) -> None:
        self.db = db
        self.bus = bus
        self.clock = clock

    # ───────────── 실계좌 / 서브계좌 ─────────────
    def ensure_broker_account(self, alias: str, env: str) -> None:
        """실계좌 등록 + 미배정 시스템 서브계좌 자동 생성 (이미 있으면 그대로)."""
        with tx(self.db, self.bus) as (s, ev):
            if s.get(BrokerAccountRow, alias) is None:
                s.add(BrokerAccountRow(id=alias, env=env, created_at=self.clock()))
                s.flush()
            if self._find(s, alias, UNALLOCATED) is None:
                s.add(SubAccountRow(broker_account_id=alias, name=UNALLOCATED, type="system", created_at=self.clock()))

    def create_sub_account(
        self, broker_account_id: str, name: str, type_: str = "strategy",
        markets: list[str] | None = None, fx_mode: str = "none",
    ) -> SubAccount:
        """markets: 거래 가능한 시장 (기본 KR, US). fx_mode: none(통화별 독립 잔고) / broker(통합증거금 자동환전)."""
        markets = list(markets or MARKETS)
        if type_ not in SUB_TYPES:
            raise OmsError("INVALID_REQUEST", f"type 은 {SUB_TYPES} 중 하나")
        if name == UNALLOCATED:
            raise OmsError("INVALID_REQUEST", "예약된 이름")
        if not markets or not set(markets) <= set(MARKETS):
            raise OmsError("INVALID_REQUEST", f"markets 는 {MARKETS} 의 부분집합")
        if fx_mode not in FX_MODES:
            raise OmsError("INVALID_REQUEST", f"fx_mode 는 {FX_MODES} 중 하나")
        with tx(self.db, self.bus) as (s, ev):
            if s.get(BrokerAccountRow, broker_account_id) is None:
                raise OmsError("NOT_FOUND", f"실계좌 {broker_account_id} 없음")
            if self._find(s, broker_account_id, name) is not None:
                raise OmsError("DUPLICATE", f"이미 있는 서브계좌 이름: {name}")
            row = SubAccountRow(broker_account_id=broker_account_id, name=name, type=type_, markets=markets,
                                fx_mode=fx_mode, created_at=self.clock())
            s.add(row)
            s.flush()
            record_event(s, ev, "SUB_ACCOUNT_CREATED", row.id,
                         {"name": name, "type": type_, "markets": markets, "fx_mode": fx_mode}, self.clock())
            return _view(row)

    def get(self, sub_account_id: int) -> SubAccount:
        with self.db.session() as s:
            return _view(self._get(s, sub_account_id))

    def list(self, broker_account_id: str | None = None) -> list[SubAccount]:
        with self.db.session() as s:
            q = select(SubAccountRow).order_by(SubAccountRow.id)
            if broker_account_id:
                q = q.where(SubAccountRow.broker_account_id == broker_account_id)
            return [_view(r) for r in s.scalars(q)]

    def unallocated(self, broker_account_id: str) -> SubAccount:
        with self.db.session() as s:
            row = self._find(s, broker_account_id, UNALLOCATED)
            if row is None:
                raise OmsError("NOT_FOUND", f"실계좌 {broker_account_id} 미등록")
            return _view(row)

    def set_status(self, sub_account_id: int, status: str) -> SubAccount:
        if status not in SUB_STATUSES:
            raise OmsError("INVALID_REQUEST", f"status 는 {SUB_STATUSES} 중 하나")
        with tx(self.db, self.bus) as (s, ev):
            row = self._get(s, sub_account_id)
            if row.type == "system":
                raise OmsError("INVALID_REQUEST", "시스템 서브계좌 상태는 변경 불가")
            row.status = status
            record_event(s, ev, "SUB_ACCOUNT_STATUS", row.id, {"status": status}, self.clock())
            return _view(row)

    # ───────────── 조회 ─────────────
    def balances(self, sub_account_id: int) -> dict[str, CashBalance]:
        with self.db.session() as s:
            self._get(s, sub_account_id)
            return {
                c: CashBalance(c, ledger.cash_balance(s, sub_account_id, c), ledger.held_cash(s, sub_account_id, c))
                for c in CURRENCIES
            }

    def positions(self, sub_account_id: int, include_closed: bool = False) -> list[Position]:
        with self.db.session() as s:
            rows = s.scalars(select(PositionRow).where(PositionRow.sub_account_id == sub_account_id))
            return [
                Position(r.instrument_id, r.currency, r.qty, ledger.available_qty(s, sub_account_id, r.instrument_id),
                         r.avg_price, r.realized_pnl)
                for r in rows if include_closed or r.qty != 0
            ]

    # ───────────── 자금 이동 ─────────────
    def deposit_unallocated(self, broker_account_id: str, currency: str, amount: Decimal, memo: str = "") -> None:
        """실계좌에 들어온 자금을 미배정으로 반영 (입금, 초기 잔고 동기화)."""
        self._check_currency(currency)
        with tx(self.db, self.bus) as (s, ev):
            un = self._find(s, broker_account_id, UNALLOCATED)
            if un is None:
                raise OmsError("NOT_FOUND", f"실계좌 {broker_account_id} 미등록")
            kind = "DEPOSIT" if amount >= 0 else "WITHDRAW"
            ledger.post(s, un.id, currency, kind, q_cash(amount, currency), at=self.clock(), memo=memo or None)

    def transfer(self, from_id: int, to_id: int, currency: str, amount: Decimal, memo: str = "") -> None:
        self._check_currency(currency)
        amount = q_cash(amount, currency)
        if amount <= 0:
            raise OmsError("INVALID_REQUEST", "이체 금액은 0보다 커야 함")
        with tx(self.db, self.bus) as (s, ev):
            src, dst = self._get(s, from_id), self._get(s, to_id)
            if src.broker_account_id != dst.broker_account_id:
                raise OmsError("INVALID_REQUEST", "다른 실계좌 사이 이체 불가")
            if ledger.available_cash(s, from_id, currency) < amount:
                raise OmsError("INSUFFICIENT_CASH", f"{src.name} 이체 가능 {currency} 부족")
            now = self.clock()
            out = ledger.post(s, from_id, currency, "TRANSFER_OUT", -amount, at=now, ref_type="sub", ref_id=to_id, memo=memo or None)
            ledger.post(s, to_id, currency, "TRANSFER_IN", amount, at=now, ref_type="ledger", ref_id=out.id, memo=memo or None)
            record_event(s, ev, "TRANSFER", from_id, {"to": to_id, "currency": currency, "amount": str(amount)}, now)

    def record_fx(self, sub_account_id: int, from_currency: str, from_amount: Decimal, to_currency: str, to_amount: Decimal) -> None:
        """HTS 등에서 직접 환전한 결과를 서브계좌에 반영."""
        self._check_currency(from_currency)
        self._check_currency(to_currency)
        with tx(self.db, self.bus) as (s, ev):
            self._get(s, sub_account_id)
            if ledger.available_cash(s, sub_account_id, from_currency) < from_amount:
                raise OmsError("INSUFFICIENT_CASH", f"환전할 {from_currency} 부족")
            now = self.clock()
            rate = (from_amount / to_amount) if from_currency == "KRW" else (to_amount / from_amount)
            ledger.post(s, sub_account_id, from_currency, "FX", -from_amount, at=now, fx_rate=rate)
            ledger.post(s, sub_account_id, to_currency, "FX", to_amount, at=now, fx_rate=rate)

    def adjust(self, sub_account_id: int, currency: str, amount: Decimal, memo: str) -> None:
        """대사 차이, 배당, 기업행위 등 수동 보정."""
        self._check_currency(currency)
        with tx(self.db, self.bus) as (s, ev):
            self._get(s, sub_account_id)
            now = self.clock()
            ledger.post(s, sub_account_id, currency, "ADJUST", q_cash(amount, currency), at=now, memo=memo)
            record_event(s, ev, "ADJUST", sub_account_id, {"currency": currency, "amount": str(amount), "memo": memo}, now)

    def transfer_position(self, from_id: int, to_id: int, instrument_id: str, qty: int) -> None:
        """보유종목을 평균단가 그대로 다른 서브계좌로 이동 (예: 미배정 → 전략)."""
        if qty <= 0:
            raise OmsError("INVALID_REQUEST", "수량은 0보다 커야 함")
        with tx(self.db, self.bus) as (s, ev):
            src, dst = self._get(s, from_id), self._get(s, to_id)
            if src.broker_account_id != dst.broker_account_id:
                raise OmsError("INVALID_REQUEST", "다른 실계좌 사이 이동 불가")
            if ledger.available_qty(s, from_id, instrument_id) < qty:
                raise OmsError("INSUFFICIENT_POSITION", f"{src.name} 이동 가능 수량 부족")
            sp = ledger.position(s, from_id, instrument_id)
            assert sp is not None
            dp = ledger.position(s, to_id, instrument_id)
            if dp is None:
                dp = PositionRow(sub_account_id=to_id, instrument_id=instrument_id, currency=sp.currency, qty=0,
                                 avg_price=ZERO, realized_pnl=ZERO)
                s.add(dp)
            total = dp.qty + qty
            dp.avg_price = (dp.avg_price * dp.qty + sp.avg_price * qty) / total
            dp.qty = total
            sp.qty -= qty
            now = self.clock()
            sp.updated_at = dp.updated_at = now
            record_event(s, ev, "POSITION_TRANSFER", from_id, {"to": to_id, "instrument_id": instrument_id, "qty": qty}, now)

    def import_position(self, sub_account_id: int, instrument_id: str, currency: str, qty: int, avg_price: Decimal) -> None:
        """초기 동기화: 실계좌에 이미 있던 보유종목을 장부에 들여온다 (체결 기록 없이)."""
        with tx(self.db, self.bus) as (s, ev):
            self._get(s, sub_account_id)
            pos = ledger.position(s, sub_account_id, instrument_id)
            if pos is None:
                pos = PositionRow(sub_account_id=sub_account_id, instrument_id=instrument_id, currency=currency,
                                  qty=0, avg_price=ZERO, realized_pnl=ZERO)
                s.add(pos)
            total = pos.qty + qty
            pos.avg_price = (pos.avg_price * pos.qty + avg_price * qty) / total if total else ZERO
            pos.qty = total
            pos.updated_at = self.clock()
            record_event(s, ev, "POSITION_IMPORT", sub_account_id,
                         {"instrument_id": instrument_id, "qty": qty, "avg_price": str(avg_price)}, self.clock())

    def is_empty(self, sub_account_id: int) -> bool:
        with self.db.session() as s:
            has_cash = s.scalar(select(LedgerRow.id).where(LedgerRow.sub_account_id == sub_account_id).limit(1))
            has_pos = s.scalar(select(PositionRow.sub_account_id).where(PositionRow.sub_account_id == sub_account_id).limit(1))
            return has_cash is None and has_pos is None

    # ───────────── 내부 ─────────────
    @staticmethod
    def _find(s: Session, broker_account_id: str, name: str) -> SubAccountRow | None:
        return s.scalar(select(SubAccountRow).where(
            SubAccountRow.broker_account_id == broker_account_id, SubAccountRow.name == name))

    @staticmethod
    def _get(s: Session, sub_account_id: int) -> SubAccountRow:
        row = s.get(SubAccountRow, sub_account_id)
        if row is None:
            raise OmsError("NOT_FOUND", f"서브계좌 {sub_account_id} 없음")
        return row

    @staticmethod
    def _check_currency(currency: str) -> None:
        if currency not in CURRENCIES:
            raise OmsError("INVALID_REQUEST", f"지원하지 않는 통화 {currency}")
