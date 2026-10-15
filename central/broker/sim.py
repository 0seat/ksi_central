"""SimBroker: 테스트/백테스트용 가상 브로커.

실계좌 상태(통화별 현금, 종목별 수량)를 자체적으로 보유한다. 그래서 서브계좌 장부 합계와
비교하는 대사 테스트가 가능하다. 통합증거금을 켜면 USD 부족분을 KRW 로 자동 환전한다.

이벤트는 즉시 전달하지 않고 큐에 쌓는다. `await sim.drain()` 을 호출해야 OMS 로 전달된다.
이렇게 하면 접수(ack)와 체결통보의 순서를 테스트에서 통제할 수 있다.
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Callable

from central.broker.base import (
    AccountSnapshot,
    Broker,
    BrokerCancelled,
    BrokerOrderStatus,
    BrokerEvent,
    BrokerFill,
    BrokerOrderAck,
    BrokerOrderRequest,
    BrokerReject,
    BrokerUnavailable,
    Candle,
    OrderBook,
    Quote,
)
from central.core.instruments import Instrument
from central.core.market_hours import now_kst
from central.core.money import ZERO, q_cash

FeeFn = Callable[[str, str | None, str, Decimal, str], tuple[Decimal, Decimal]]


@dataclass
class SimOrder:
    no: str
    req: BrokerOrderRequest
    price: Decimal | None
    remaining: int
    open: bool = True
    filled: int = 0
    fill_value: Decimal = ZERO
    orig: str | None = None
    ordered_at: str = ""


class SimBroker(Broker):
    def __init__(
        self,
        account_id: str,
        *,
        integrated_margin: bool = False,
        fee_fn: FeeFn | None = None,
        clock: Callable[[], datetime] = now_kst,
    ) -> None:
        super().__init__(account_id)
        self.integrated_margin = integrated_margin
        self.fee_fn = fee_fn or (lambda *_: (ZERO, ZERO))
        self.clock = clock
        self.cash: dict[str, Decimal] = defaultdict(lambda: ZERO)
        self.rights: dict = {}
        self.positions: dict[str, int] = defaultdict(int)
        self.avg: dict[str, Decimal] = defaultdict(lambda: ZERO)
        self.quotes: dict[str, Quote] = {}
        self.fx: Decimal = Decimal("1400")
        self.orders: dict[str, SimOrder] = {}
        self.pending_events: list[BrokerEvent] = []
        self.reject_next: BrokerReject | None = None
        self.fail_next: bool = False          # 접수 전 실패 (주문 없음)
        self.drop_ack_next: bool = False      # 접수는 됐는데 응답이 유실됨
        self._seq = itertools.count(1)
        self._fill_seq = itertools.count(1)
        self.candles: dict[tuple[str, str], list[Candle]] = {}
        self.candle_page_size = 120
        self.candle_calls = 0
        self.kr_days: dict[date, bool] = {}
        self.market_day_calls = 0

    # ───────────── 테스트 조작 ─────────────
    def deposit(self, currency: str, amount: Decimal) -> None:
        self.cash[currency] += amount

    def pay_right(self, right, currency: str, day) -> None:
        """시험용: 배당 등 권리 현금 지급 (세후 금액이 현금에 들어오고 cash_rights 로 조회된다)."""
        self.cash[currency] += right.gross - right.tax
        self.rights.setdefault(day, []).append(right)

    async def cash_rights(self, market: str, day):
        return [r for r in self.rights.get(day, []) if r.instrument_id.startswith(f"{market}:")]

    def set_quote(self, instrument_id: str, last: Decimal, upper: Decimal | None = None, lower: Decimal | None = None) -> None:
        self.quotes[instrument_id] = Quote(last, upper, lower)

    def set_price(self, instrument: Instrument, last: Decimal) -> None:
        """시세 변경 후 체결 조건을 만족하는 미체결 주문을 전량 체결 처리한다."""
        q = self.quotes.get(instrument.id)
        self.quotes[instrument.id] = Quote(last, q.upper_limit if q else None, q.lower_limit if q else None)
        for o in list(self.orders.values()):
            if not o.open or o.req.instrument.id != instrument.id:
                continue
            crosses = o.price is None or (o.req.side == "buy" and last <= o.price) or (o.req.side == "sell" and last >= o.price)
            if crosses:
                self.fill(o.no, o.remaining, last)

    def fill(self, no: str, qty: int, price: Decimal, venue: str | None = None) -> None:
        o = self.orders[no]
        qty = min(qty, o.remaining)
        o.remaining -= qty
        o.open = o.remaining > 0
        o.filled += qty
        o.fill_value += price * qty
        self._settle(o.req.instrument, o.req.side, qty, price, venue)
        self.pending_events.append(
            BrokerFill(no, f"F{next(self._fill_seq):06d}", o.req.instrument.id, o.req.side, qty, price, venue, self.clock())
        )

    def fill_silently(self, no: str, qty: int, price: Decimal) -> None:
        """체결은 됐지만 체결통보가 유실된 상황 (동기화 테스트용)."""
        self.fill(no, qty, price)
        self.pending_events.pop()

    def external_fill(self, instrument: Instrument, side: str, qty: int, price: Decimal) -> None:
        """중앙증권을 거치지 않은 체결 (HTS/MTS 수동매매)."""
        no = f"X{next(self._seq):07d}"
        self._settle(instrument, side, qty, price, None)
        self.pending_events.append(
            BrokerFill(no, f"F{next(self._fill_seq):06d}", instrument.id, side, qty, price, None, self.clock())  # type: ignore[arg-type]
        )

    async def drain(self) -> None:
        while self.pending_events:
            await self.emit(self.pending_events.pop(0))

    def _settle(self, instrument: Instrument, side: str, qty: int, price: Decimal, venue: str | None) -> None:
        cur = instrument.currency
        notional = q_cash(price * qty, cur)
        fee, tax = self.fee_fn(instrument.market, venue, side, notional, cur)
        if side == "buy":
            cost = notional + fee + tax
            if cur == "USD" and self.integrated_margin and self.cash["USD"] < cost:
                short = cost - max(self.cash["USD"], ZERO)
                self.cash["KRW"] -= q_cash(short * self.fx, "KRW")
                self.cash["USD"] += short
            self.cash[cur] -= cost
            held = self.positions[instrument.id]
            self.avg[instrument.id] = (self.avg[instrument.id] * held + price * qty) / (held + qty)
            self.positions[instrument.id] += qty
        else:
            self.cash[cur] += notional - fee - tax
            self.positions[instrument.id] -= qty

    # ───────────── Broker 구현 ─────────────
    async def submit(self, req: BrokerOrderRequest) -> BrokerOrderAck:
        if self.fail_next:
            self.fail_next = False
            raise BrokerUnavailable("sim: 응답 없음")
        if self.reject_next:
            err, self.reject_next = self.reject_next, None
            raise err
        no = f"S{next(self._seq):07d}"
        self.orders[no] = SimOrder(no, req, req.price, req.qty, ordered_at=self.clock().strftime("%H%M%S"))
        if self.drop_ack_next:
            self.drop_ack_next = False
            raise BrokerUnavailable("sim: 접수됐지만 응답 유실")
        return BrokerOrderAck(no, "00950")

    async def cancel(
        self, broker_order_no: str, broker_org_no: str, instrument: Instrument, qty: int, route: str | None = None
    ) -> None:
        o = self.orders.get(broker_order_no)
        if o is None or not o.open:
            raise BrokerReject("SIM_NOT_OPEN", "취소 가능한 주문 아님")
        canceled, o.remaining, o.open = o.remaining, 0, False
        self.pending_events.append(BrokerCancelled(broker_order_no, canceled))

    async def amend(
        self, broker_order_no: str, broker_org_no: str, instrument: Instrument, price: Decimal, qty: int,
        route: str | None = None,
    ) -> BrokerOrderAck:
        o = self.orders.get(broker_order_no)
        if o is None or not o.open:
            raise BrokerReject("SIM_NOT_OPEN", "정정 가능한 주문 아님")
        o.remaining, o.open = 0, False
        no = f"S{next(self._seq):07d}"
        req = BrokerOrderRequest(o.req.instrument, o.req.side, "limit", qty, price, o.req.route)
        self.orders[no] = SimOrder(no, req, price, qty, orig=broker_order_no, ordered_at=self.clock().strftime("%H%M%S"))
        return BrokerOrderAck(no, broker_org_no)

    async def order_statuses(self, market: str, day: date) -> list[BrokerOrderStatus]:
        return [
            BrokerOrderStatus(
                o.no, o.req.instrument.id, o.req.side, o.req.qty, o.price, o.filled,
                (o.fill_value / o.filled) if o.filled else None, o.remaining, o.orig, o.ordered_at,
            )
            for o in self.orders.values() if o.req.instrument.market == market
        ]

    async def snapshot(self, market: str) -> AccountSnapshot:
        cur = "KRW" if market == "KR" else "USD"
        positions = {
            k: (v, self.avg[k]) for k, v in self.positions.items() if v and k.startswith(f"{market}:")
        }
        return AccountSnapshot(market, {cur: self.cash[cur]}, positions, self.fx)

    async def quote(self, instrument: Instrument) -> Quote:
        q = self.quotes.get(instrument.id)
        if q is None:
            raise BrokerUnavailable(f"sim: {instrument.id} 시세 없음")
        return q

    async def fx_rate(self, pair: str = "USDKRW") -> Decimal:
        return self.fx

    async def candle_page(self, instrument: Instrument, interval: str, end: datetime) -> list[Candle]:
        """set_candles 로 넣은 봉 중 end 이전 최근 page_size 개 (KIS 처럼 페이지 단위)."""
        self.candle_calls += 1
        bars = [b for b in self.candles.get((instrument.id, interval), []) if b.ts <= end]
        return sorted(bars, key=lambda b: b.ts)[-self.candle_page_size:]

    async def market_days(self, start: date, pages: int = 3) -> list[tuple[date, bool]]:
        self.market_day_calls += 1
        return [(d, o) for d, o in sorted(self.kr_days.items()) if d >= start]

    async def orderbook(self, instrument: Instrument) -> OrderBook:
        q = self.quotes.get(instrument.id)
        if q is None:
            raise BrokerUnavailable(f"sim: {instrument.id} 시세 없음")
        return OrderBook(self.clock(), [(q.last, 10)], [(q.last, 10)])
