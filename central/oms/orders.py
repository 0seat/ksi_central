"""주문 처리 (docs/PLAN.md 6.4 ~ 6.7).

흐름: 검증 → 리스크 → 홀드 → 저장(SUBMITTING) → 브로커 접수(ACCEPTED) → 체결통보로 원장/포지션 반영

동시성: 실계좌별 asyncio.Lock 하나로 주문 접수와 브로커 이벤트 처리를 직렬화한다.
체결통보가 접수 응답보다 먼저 도착해도 락 때문에 접수 기록이 끝난 뒤 처리된다.

환전(6.2): 기본(fx_mode=none)은 통화별 독립 잔고라 USD 가 부족하면 미국 주문을 거부한다.
서브계좌가 fx_mode=broker 이고 실계좌가 통합증거금이면, USD 부족분을 KRW 로 홀드하고(환율 x (1+fx_buffer))
체결 시 부족분만큼 FX_AUTO(KRW 차감, USD 증가)를 잠정 기록한다.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from central.broker.base import (
    Broker,
    BrokerCancelled,
    BrokerEvent,
    BrokerFill,
    BrokerOrderRejected,
    BrokerOrderRequest,
    BrokerReject,
    BrokerUnavailable,
)
from central.core.events import EventBus
from central.core.instruments import Instrument, InstrumentRegistry, is_tick_aligned
from central.core.market_hours import MarketHours, now_kst
from central.core.money import ZERO, q_cash, q_hold, q_price
from central.oms import ledger
from central.oms.accounts import UNALLOCATED, record_event, tx
from central.oms.errors import OmsError
from central.oms.fees import FeeModel
from central.risk.engine import RiskEngine
from central.settings import AccountProfile
from central.storage.db import Database
from central.storage.models import FillRow, HoldRow, OrderRow, PositionRow, SubAccountRow

log = logging.getLogger(__name__)

TERMINAL = ("FILLED", "CANCELED", "REJECTED", "SUBMIT_FAILED", "EXPIRED", "REPLACED")
WORKING = ("ACCEPTED", "PARTIALLY_FILLED")
EXPIRABLE = ("ACCEPTED", "PARTIALLY_FILLED", "CANCEL_PENDING")
DEFAULT_FX_BUFFER = Decimal("0.02")


@dataclass(frozen=True)
class PlaceOrder:
    client_order_id: str
    symbol: str
    side: str            # buy / sell
    order_type: str      # limit / market
    qty: int
    price: Decimal | None = None
    route: str | None = None


@dataclass(frozen=True)
class OrderView:
    id: int
    sub_account_id: int
    client_order_id: str
    symbol: str
    side: str
    order_type: str
    route: str | None
    price: Decimal | None
    qty: int
    filled_qty: int
    avg_fill_price: Decimal | None
    status: str
    reject_reason: str | None
    orig_order_id: int | None


def _view(o: OrderRow) -> OrderView:
    return OrderView(o.id, o.sub_account_id, o.client_order_id, o.instrument_id, o.side, o.order_type, o.route,
                     o.price, o.qty, o.filled_qty, o.avg_fill_price, o.status, o.reject_reason, o.orig_order_id)


@dataclass(frozen=True)
class _HoldPlan:
    """주문 하나에 필요한 홀드 목록: (kind, currency, instrument_id, amount, fx_rate)"""
    items: list[tuple[str, str | None, str | None, Decimal, Decimal | None]]


class OrderService:
    def __init__(
        self,
        db: Database,
        bus: EventBus,
        instruments: InstrumentRegistry,
        brokers: dict[str, Broker],
        profiles: dict[str, AccountProfile],
        fees: FeeModel,
        risk: RiskEngine,
        hours: MarketHours,
        *,
        fx_buffer: Decimal = DEFAULT_FX_BUFFER,
        clock: Callable[[], datetime] = now_kst,
    ) -> None:
        self.db, self.bus, self.instruments = db, bus, instruments
        self.brokers, self.profiles = brokers, profiles
        self.fees, self.risk, self.hours = fees, risk, hours
        self.fx_buffer = fx_buffer
        self.clock = clock
        self._locks = {alias: asyncio.Lock() for alias in brokers}
        for alias, broker in brokers.items():
            broker.set_event_handler(lambda ev, alias=alias: self.on_broker_event(alias, ev))

    # ───────────── 조회 ─────────────
    def get(self, sub_account_id: int, order_id: int) -> OrderView:
        with self.db.session() as s:
            return _view(self._own_order(s, sub_account_id, order_id))

    def list(self, sub_account_id: int, status: str | None = None) -> list[OrderView]:
        with self.db.session() as s:
            q = select(OrderRow).where(OrderRow.sub_account_id == sub_account_id).order_by(OrderRow.id)
            if status:
                q = q.where(OrderRow.status == status)
            return [_view(o) for o in s.scalars(q)]

    # ───────────── 주문 ─────────────
    async def place(self, sub_account_id: int, req: PlaceOrder) -> OrderView:
        broker_alias = self._broker_of(sub_account_id)
        async with self._locks[broker_alias]:
            existing = self._find_client_order(sub_account_id, req.client_order_id)
            if existing is not None:
                return existing  # 멱등성: 같은 client_order_id 는 기존 주문 반환

            instrument, route = self._validate(broker_alias, sub_account_id, req)
            broker = self.brokers[broker_alias]
            est_price = req.price
            if req.order_type == "market":
                quote = await self._quote(broker, instrument)
                if quote.upper_limit is None:
                    raise OmsError("BROKER_UNAVAILABLE", "시장가 홀드 계산에 필요한 상한가 없음")
                est_price = quote.upper_limit
            assert est_price is not None
            fx = await broker.fx_rate() if instrument.currency == "USD" else None

            now = self.clock()
            with tx(self.db, self.bus) as (s, ev):
                self.risk.check(s, broker_account_id=broker_alias, sub_account_id=sub_account_id,
                                instrument=instrument, side=req.side, qty=req.qty, est_price=est_price, now=now)
                plan = self._hold_plan(s, broker_alias, sub_account_id, instrument, req.side, req.qty, est_price, fx, now)
                order = OrderRow(
                    sub_account_id=sub_account_id, broker_account_id=broker_alias, client_order_id=req.client_order_id,
                    instrument_id=instrument.id, market=instrument.market, currency=instrument.currency,
                    side=req.side, order_type=req.order_type, route=route, price=req.price, qty=req.qty,
                    status="SUBMITTING", created_at=now, updated_at=now,
                )
                s.add(order)
                s.flush()
                self._create_holds(s, sub_account_id, order.id, plan)
                order_id = order.id

            try:
                ack = await broker.submit(BrokerOrderRequest(instrument, req.side, req.order_type, req.qty, req.price, route))  # type: ignore[arg-type]
            except BrokerReject as e:
                return self._finish_rejected(order_id, "REJECTED", f"[{e.code}] {e.message}")
            except BrokerUnavailable as e:
                return self._mark_unknown(order_id, str(e))
            except Exception as e:  # 예상하지 못한 오류도 접수 여부를 알 수 없으므로 UNKNOWN
                log.exception("주문 접수 중 예외")
                return self._mark_unknown(order_id, f"{type(e).__name__}: {e}")

            with tx(self.db, self.bus) as (s, ev):
                order = s.get(OrderRow, order_id)
                assert order is not None
                order.broker_order_no, order.broker_org_no = ack.broker_order_no, ack.broker_org_no
                order.status, order.updated_at = "ACCEPTED", self.clock()
                record_event(s, ev, "ORDER", sub_account_id, self._payload(order), order.updated_at)
                return _view(order)

    async def cancel(self, sub_account_id: int, order_id: int) -> OrderView:
        broker_alias = self._broker_of(sub_account_id)
        async with self._locks[broker_alias]:
            with tx(self.db, self.bus) as (s, ev):
                order = self._own_order(s, sub_account_id, order_id)
                if order.status not in WORKING:
                    raise OmsError("ORDER_NOT_CANCELABLE", f"현재 상태 {order.status}")
                prev_status = order.status
                order.status, order.updated_at = "CANCEL_PENDING", self.clock()
                record_event(s, ev, "ORDER", sub_account_id, self._payload(order), order.updated_at)
                no, org = order.broker_order_no, order.broker_org_no or ""
                remaining = order.qty - order.filled_qty
                instrument = self._instrument(order.instrument_id)
            try:
                assert no is not None
                await self.brokers[broker_alias].cancel(no, org, instrument, remaining)
            except BrokerReject as e:
                with tx(self.db, self.bus) as (s, ev):
                    order = s.get(OrderRow, order_id)
                    assert order is not None
                    order.status, order.updated_at = prev_status, self.clock()
                raise OmsError("ORDER_NOT_CANCELABLE", f"브로커 거부 [{e.code}] {e.message}") from None
            return self.get(sub_account_id, order_id)

    async def amend(self, sub_account_id: int, order_id: int, price: Decimal, qty: int | None = None) -> OrderView:
        """정정: 기존 주문은 REPLACED, 잔량(또는 지정 수량)으로 새 주문을 만든다 (KIS 정정은 새 주문번호 발급)."""
        broker_alias = self._broker_of(sub_account_id)
        async with self._locks[broker_alias]:
            with self.db.session() as s:
                old = self._own_order(s, sub_account_id, order_id)
                if old.status not in WORKING or old.order_type != "limit":
                    raise OmsError("ORDER_NOT_CANCELABLE", f"정정 불가 상태 {old.status}/{old.order_type}")
                instrument = self._instrument(old.instrument_id)
                new_qty = qty if qty is not None else old.qty - old.filled_qty
                if new_qty <= 0 or new_qty > old.qty - old.filled_qty:
                    raise OmsError("INVALID_REQUEST", "정정 수량은 1 이상, 잔량 이하")
                if not is_tick_aligned(instrument, price):
                    raise OmsError("INVALID_TICK_SIZE", f"{price} 는 호가단위에 맞지 않음")
                no, org, side, route = old.broker_order_no, old.broker_org_no or "", old.side, old.route
                client_id, n_amend = old.client_order_id, self._amend_count(s, old)

            broker = self.brokers[broker_alias]
            fx = await broker.fx_rate() if instrument.currency == "USD" else None
            now = self.clock()
            with self.db.session() as s:  # 사전 점검만 (기존 주문 홀드는 해제된다고 보고 계산)
                self._hold_plan(s, broker_alias, sub_account_id, instrument, side, new_qty, price, fx, now, exclude_order_id=order_id)

            assert no is not None
            try:
                ack = await broker.amend(no, org, instrument, price, new_qty)
            except BrokerReject as e:
                raise OmsError("BROKER_REJECTED", f"[{e.code}] {e.message}") from None

            with tx(self.db, self.bus) as (s, ev):
                old = s.get(OrderRow, order_id)
                assert old is not None
                ledger.release_holds(s, old.id)
                old.status, old.updated_at = "REPLACED", now
                plan = self._hold_plan(s, broker_alias, sub_account_id, instrument, side, new_qty, price, fx, now)
                new = OrderRow(
                    sub_account_id=sub_account_id, broker_account_id=broker_alias,
                    client_order_id=f"{client_id}#A{n_amend + 1}", broker_order_no=ack.broker_order_no,
                    broker_org_no=ack.broker_org_no, orig_order_id=old.id, instrument_id=instrument.id,
                    market=instrument.market, currency=instrument.currency, side=side, order_type="limit",
                    route=route, price=price, qty=new_qty, status="ACCEPTED", created_at=now, updated_at=now,
                )
                s.add(new)
                s.flush()
                self._create_holds(s, sub_account_id, new.id, plan)
                record_event(s, ev, "ORDER", sub_account_id, self._payload(old), now)
                record_event(s, ev, "ORDER", sub_account_id, self._payload(new), now)
                return _view(new)

    def expire_open_orders(self, broker_account_id: str, market: str) -> int:
        """장 종료 후 미체결 잔량 소멸 처리. 접수 여부가 불확실한 UNKNOWN/SUBMITTING 은 건드리지 않는다."""
        with tx(self.db, self.bus) as (s, ev):
            rows = list(s.scalars(select(OrderRow).where(
                OrderRow.broker_account_id == broker_account_id, OrderRow.market == market,
                OrderRow.status.in_(EXPIRABLE))))
            now = self.clock()
            for o in rows:
                ledger.release_holds(s, o.id)
                o.status, o.updated_at = "EXPIRED", now
                record_event(s, ev, "ORDER", o.sub_account_id, self._payload(o), now)
            return len(rows)

    # ───────────── 브로커 이벤트 ─────────────
    async def on_broker_event(self, broker_alias: str, event: BrokerEvent) -> None:
        async with self._locks[broker_alias]:
            if isinstance(event, BrokerFill):
                fx = None
                inst = self.instruments.get(event.instrument_id)
                if inst is not None and inst.currency == "USD":
                    fx = await self.brokers[broker_alias].fx_rate()
                self._apply_fill(broker_alias, event, fx)
            elif isinstance(event, BrokerCancelled):
                self._apply_cancel(broker_alias, event)
            elif isinstance(event, BrokerOrderRejected):
                self._apply_late_reject(broker_alias, event)

    def _apply_fill(self, broker_alias: str, f: BrokerFill, fx: Decimal | None) -> None:
        with tx(self.db, self.bus) as (s, ev):
            if s.scalar(select(FillRow.id).where(FillRow.broker_account_id == broker_alias,
                                                 FillRow.broker_fill_key == f.fill_key)) is not None:
                log.info("중복 체결통보 무시: %s", f.fill_key)
                return
            now = self.clock()
            order = self._order_by_broker_no(s, broker_alias, f.broker_order_no)
            if order is None:
                sub_id = self._unallocated_id(s, broker_alias)
                instrument = self._instrument(f.instrument_id)
                self._book_fill(s, broker_alias, sub_id, None, instrument, f, fx, now)
                record_event(s, ev, "UNMATCHED_FILL", sub_id, {
                    "broker_order_no": f.broker_order_no, "instrument_id": f.instrument_id,
                    "side": f.side, "qty": f.qty, "price": str(f.price)}, now)
                log.warning("매핑 없는 체결 → 미배정 귀속: %s %s %s", f.instrument_id, f.side, f.qty)
                return

            remaining = order.qty - order.filled_qty
            qty = f.qty
            if qty > remaining:
                log.error("체결 수량이 잔량 초과: order=%s fill=%s remaining=%s", order.id, qty, remaining)
                qty = remaining
            instrument = self._instrument(order.instrument_id)
            fill = BrokerFill(f.broker_order_no, f.fill_key, f.instrument_id, f.side, qty, f.price, f.venue, f.filled_at)
            self._book_fill(s, broker_alias, order.sub_account_id, order, instrument, fill, fx, now)

            prev_value = (order.avg_fill_price or ZERO) * order.filled_qty
            order.filled_qty += qty
            order.avg_fill_price = q_price((prev_value + f.price * qty) / order.filled_qty)
            self._reduce_holds(s, order, qty)
            if order.filled_qty >= order.qty:
                order.status = "FILLED"
                ledger.release_holds(s, order.id)
            elif order.status != "CANCEL_PENDING":
                order.status = "PARTIALLY_FILLED"
            order.updated_at = now
            record_event(s, ev, "FILL", order.sub_account_id, {
                "order_id": order.id, "qty": qty, "price": str(f.price), "venue": f.venue}, now)
            record_event(s, ev, "ORDER", order.sub_account_id, self._payload(order), now)

    def _book_fill(
        self, s: Session, broker_alias: str, sub_id: int, order: OrderRow | None, instrument: Instrument,
        f: BrokerFill, fx: Decimal | None, now: datetime,
    ) -> None:
        cur = instrument.currency
        notional = q_cash(f.price * f.qty, cur)
        fee, tax = self.fees.compute(broker_alias, instrument.market, f.venue, f.side, notional, cur, now.date())
        pos = ledger.position(s, sub_id, instrument.id)
        if pos is None:
            pos = PositionRow(sub_account_id=sub_id, instrument_id=instrument.id, currency=cur, qty=0,
                              avg_price=ZERO, realized_pnl=ZERO)
            s.add(pos)
        realized = ZERO
        order_id = order.id if order else None

        if f.side == "buy":
            cost = notional + fee + tax
            if cur == "USD" and self._auto_fx(s, broker_alias, sub_id):
                other_holds = ledger.held_cash(s, sub_id, "USD", exclude_order_id=order_id)
                usable = ledger.cash_balance(s, sub_id, "USD") - other_holds
                if usable < cost:
                    short = cost - max(usable, ZERO)
                    rate = fx or ZERO
                    krw = q_cash(short * rate, "KRW")
                    ledger.post(s, sub_id, "KRW", "FX_AUTO", -krw, at=now, ref_type="order", ref_id=order_id,
                                fx_rate=rate, provisional=True, memo="통합증거금 자동환전(잠정)")
                    ledger.post(s, sub_id, "USD", "FX_AUTO", short, at=now, ref_type="order", ref_id=order_id,
                                fx_rate=rate, provisional=True, memo="통합증거금 자동환전(잠정)")
            ledger.post(s, sub_id, cur, "BUY", -notional, at=now, ref_type="order", ref_id=order_id)
            total = pos.qty + f.qty
            pos.avg_price = q_price((pos.avg_price * pos.qty + f.price * f.qty) / total) if total else ZERO
            pos.qty = total
        else:
            ledger.post(s, sub_id, cur, "SELL", notional, at=now, ref_type="order", ref_id=order_id)
            realized = q_cash((f.price - pos.avg_price) * f.qty, cur) - fee - tax
            pos.qty -= f.qty
            pos.realized_pnl += realized
            if pos.qty == 0:
                pos.avg_price = ZERO
            if pos.qty < 0:
                log.error("포지션 음수: sub=%s %s qty=%s", sub_id, instrument.id, pos.qty)
        if fee:
            ledger.post(s, sub_id, cur, "FEE", -fee, at=now, ref_type="order", ref_id=order_id)
        if tax:
            ledger.post(s, sub_id, cur, "TAX", -tax, at=now, ref_type="order", ref_id=order_id)
        pos.updated_at = now
        s.add(FillRow(
            broker_account_id=broker_alias, broker_fill_key=f.fill_key, order_id=order_id, sub_account_id=sub_id,
            instrument_id=instrument.id, venue=f.venue, side=f.side, price=f.price, qty=f.qty, fee=fee, tax=tax,
            realized_pnl=realized, currency=cur, filled_at=f.filled_at,
        ))

    def _apply_cancel(self, broker_alias: str, e: BrokerCancelled) -> None:
        with tx(self.db, self.bus) as (s, ev):
            order = self._order_by_broker_no(s, broker_alias, e.broker_order_no)
            if order is None or order.status in TERMINAL:
                return
            ledger.release_holds(s, order.id)
            order.status, order.updated_at = "CANCELED", self.clock()
            record_event(s, ev, "ORDER", order.sub_account_id, self._payload(order), order.updated_at)

    def _apply_late_reject(self, broker_alias: str, e: BrokerOrderRejected) -> None:
        with tx(self.db, self.bus) as (s, ev):
            order = self._order_by_broker_no(s, broker_alias, e.broker_order_no)
            if order is None or order.status in TERMINAL:
                return
            ledger.release_holds(s, order.id)
            order.status, order.reject_reason, order.updated_at = "REJECTED", e.reason[:200], self.clock()
            record_event(s, ev, "ORDER", order.sub_account_id, self._payload(order), order.updated_at)

    # ───────────── 검증 / 홀드 ─────────────
    def _validate(self, broker_alias: str, sub_account_id: int, req: PlaceOrder) -> tuple[Instrument, str | None]:
        profile = self.profiles[broker_alias]
        with self.db.session() as s:
            sub = s.get(SubAccountRow, sub_account_id)
            assert sub is not None
            if sub.type == "system":
                raise OmsError("INVALID_REQUEST", "시스템 서브계좌로는 주문 불가")
            if sub.status != "active":
                raise OmsError("SUB_ACCOUNT_SUSPENDED", f"서브계좌 상태 {sub.status}")
            sub_markets = list(sub.markets)
        if not profile.trading_enabled:
            raise OmsError("ACCOUNT_TRADING_DISABLED", f"실계좌 {broker_alias} 거래 비활성")
        if not req.client_order_id or len(req.client_order_id) > 48:
            raise OmsError("INVALID_REQUEST", "client_order_id 는 1~48자")
        instrument = self.instruments.get(req.symbol)
        if instrument is None:
            raise OmsError("INVALID_SYMBOL", req.symbol)
        if instrument.market not in profile.markets:
            raise OmsError("MARKET_NOT_SUPPORTED", f"{broker_alias} 계좌는 {instrument.market} 미지원")
        if instrument.market not in sub_markets:
            raise OmsError("MARKET_NOT_SUPPORTED", f"서브계좌 거래 시장 {sub_markets} 밖의 주문")
        if req.side not in ("buy", "sell"):
            raise OmsError("INVALID_REQUEST", "side 는 buy/sell")
        if not isinstance(req.qty, int) or req.qty <= 0:
            raise OmsError("INVALID_REQUEST", "qty 는 1 이상 정수")

        allowed_types = ("limit", "market") if instrument.market == "KR" else ("limit",)
        if req.order_type not in allowed_types:
            raise OmsError("UNSUPPORTED_ORDER_TYPE", f"{instrument.market} 지원 주문유형 {allowed_types}")
        if req.order_type == "limit":
            if req.price is None or not is_tick_aligned(instrument, req.price):
                raise OmsError("INVALID_TICK_SIZE", f"{req.price} 는 호가단위에 맞지 않음")
        elif req.price is not None:
            raise OmsError("INVALID_REQUEST", "시장가 주문에는 price 를 넣지 않음")

        route: str | None = None
        if instrument.market == "KR":
            route = req.route or profile.kr_default_route
            if route not in profile.kr_routes:
                raise OmsError("UNSUPPORTED_ROUTE", f"{broker_alias} 계좌 지원 경로 {profile.kr_routes}")
            if route == "NXT" and not instrument.nxt_tradable:
                raise OmsError("UNSUPPORTED_ROUTE", f"{instrument.id} 는 NXT 거래 불가")
        elif req.route is not None:
            raise OmsError("UNSUPPORTED_ROUTE", "route 는 국내 주문에만 사용")

        ok, reason = self.hours.can_place_order(instrument.market, self.clock())
        if not ok:
            raise OmsError("MARKET_CLOSED", reason)
        return instrument, route

    def _hold_plan(
        self, s: Session, broker_alias: str, sub_id: int, instrument: Instrument, side: str, qty: int,
        price: Decimal, fx: Decimal | None, now: datetime, exclude_order_id: int | None = None,
    ) -> _HoldPlan:
        if side == "sell":
            if ledger.available_qty(s, sub_id, instrument.id, exclude_order_id) < qty:
                raise OmsError("INSUFFICIENT_POSITION", f"{instrument.id} 매도 가능 수량 부족")
            return _HoldPlan([("qty", None, instrument.id, Decimal(qty), None)])

        cur = instrument.currency
        rate = self.fees.max_rate(broker_alias, instrument.market, "buy", now.date())
        need = q_hold(price * qty * (1 + rate), cur)
        available = ledger.available_cash(s, sub_id, cur, exclude_order_id)
        if available >= need:
            return _HoldPlan([("cash", cur, None, need, None)])
        if cur == "USD" and fx and self._auto_fx(s, broker_alias, sub_id):
            usd_part = max(available, ZERO)
            hold_rate = fx * (1 + self.fx_buffer)
            krw_need = q_hold((need - usd_part) * hold_rate, "KRW")
            if ledger.available_cash(s, sub_id, "KRW", exclude_order_id) < krw_need:
                raise OmsError("INSUFFICIENT_CASH", f"USD 부족분을 충당할 KRW 부족 (필요 {krw_need})")
            items = [("cash", "KRW", None, krw_need, hold_rate)]
            if usd_part > 0:
                items.insert(0, ("cash", "USD", None, usd_part, None))
            return _HoldPlan(items)
        raise OmsError("INSUFFICIENT_CASH", f"주문가능 {cur} 부족 (필요 {need}, 가능 {available})")

    def _auto_fx(self, s: Session, broker_alias: str, sub_id: int) -> bool:
        """USD 부족분을 KRW 로 충당(통합증거금 자동환전)할지: 실계좌가 통합증거금이고 서브계좌가 fx_mode=broker 일 때만."""
        sub = s.get(SubAccountRow, sub_id)
        return self.profiles[broker_alias].integrated_margin and sub is not None and sub.fx_mode == "broker"

    @staticmethod
    def _create_holds(s: Session, sub_id: int, order_id: int, plan: _HoldPlan) -> None:
        for kind, cur, inst, amount, fx_rate in plan.items:
            s.add(HoldRow(sub_account_id=sub_id, order_id=order_id, kind=kind, currency=cur, instrument_id=inst,
                          original=amount, remaining=amount, fx_rate=fx_rate))

    @staticmethod
    def _reduce_holds(s: Session, order: OrderRow, fill_qty: int) -> None:
        """체결 수량 비율만큼 홀드를 줄인다 (잔여분은 주문 종료 시 일괄 해제)."""
        for h in ledger.order_holds(s, order.id):
            if h.kind == "qty":
                h.remaining = max(ZERO, h.remaining - fill_qty)
            else:
                assert h.currency is not None
                h.remaining = max(ZERO, h.remaining - q_hold(h.original * fill_qty / order.qty, h.currency))

    # ───────────── 공통 헬퍼 ─────────────
    def _finish_rejected(self, order_id: int, status: str, reason: str) -> OrderView:
        with tx(self.db, self.bus) as (s, ev):
            order = s.get(OrderRow, order_id)
            assert order is not None
            ledger.release_holds(s, order.id)
            order.status, order.reject_reason, order.updated_at = status, reason[:200], self.clock()
            record_event(s, ev, "ORDER", order.sub_account_id, self._payload(order), order.updated_at)
            return _view(order)

    def _mark_unknown(self, order_id: int, reason: str) -> OrderView:
        with tx(self.db, self.bus) as (s, ev):
            order = s.get(OrderRow, order_id)
            assert order is not None
            order.status, order.reject_reason, order.updated_at = "UNKNOWN", reason[:200], self.clock()
            record_event(s, ev, "ORDER_UNKNOWN", order.sub_account_id, self._payload(order), order.updated_at)
            return _view(order)

    async def _quote(self, broker: Broker, instrument: Instrument):
        try:
            return await broker.quote(instrument)
        except BrokerUnavailable as e:
            raise OmsError("BROKER_UNAVAILABLE", str(e)) from None

    def _broker_of(self, sub_account_id: int) -> str:
        with self.db.session() as s:
            sub = s.get(SubAccountRow, sub_account_id)
            if sub is None:
                raise OmsError("NOT_FOUND", f"서브계좌 {sub_account_id} 없음")
            if sub.broker_account_id not in self.brokers:
                raise OmsError("BROKER_UNAVAILABLE", f"실계좌 {sub.broker_account_id} 브로커 미연결")
            return sub.broker_account_id

    def _find_client_order(self, sub_account_id: int, client_order_id: str) -> OrderView | None:
        with self.db.session() as s:
            o = s.scalar(select(OrderRow).where(
                OrderRow.sub_account_id == sub_account_id, OrderRow.client_order_id == client_order_id))
            return _view(o) if o else None

    def _instrument(self, instrument_id: str) -> Instrument:
        inst = self.instruments.get(instrument_id)
        if inst is None:
            raise OmsError("INVALID_SYMBOL", instrument_id)
        return inst

    @staticmethod
    def _own_order(s: Session, sub_account_id: int, order_id: int) -> OrderRow:
        o = s.get(OrderRow, order_id)
        if o is None or o.sub_account_id != sub_account_id:
            raise OmsError("ORDER_NOT_FOUND", str(order_id))
        return o

    @staticmethod
    def _order_by_broker_no(s: Session, broker_alias: str, broker_order_no: str) -> OrderRow | None:
        # KIS 주문번호는 일자별로 재사용될 수 있으므로 가장 최근 주문을 택한다
        return s.scalar(select(OrderRow).where(
            OrderRow.broker_account_id == broker_alias, OrderRow.broker_order_no == broker_order_no,
        ).order_by(OrderRow.id.desc()).limit(1))

    @staticmethod
    def _unallocated_id(s: Session, broker_alias: str) -> int:
        sub_id = s.scalar(select(SubAccountRow.id).where(
            SubAccountRow.broker_account_id == broker_alias, SubAccountRow.name == UNALLOCATED))
        assert sub_id is not None
        return sub_id

    @staticmethod
    def _amend_count(s: Session, order: OrderRow) -> int:
        n, cur = 0, order
        while cur.orig_order_id is not None:
            n += 1
            prev = s.get(OrderRow, cur.orig_order_id)
            if prev is None:
                break
            cur = prev
        return n

    @staticmethod
    def _payload(o: OrderRow) -> dict:
        return {
            "id": o.id, "client_order_id": o.client_order_id, "symbol": o.instrument_id, "side": o.side,
            "order_type": o.order_type, "route": o.route, "price": str(o.price) if o.price is not None else None,
            "qty": o.qty, "filled_qty": o.filled_qty, "status": o.status, "reject_reason": o.reject_reason,
        }
