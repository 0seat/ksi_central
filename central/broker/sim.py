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
from datetime import datetime
from decimal import Decimal
from typing import Callable

from central.broker.base import (
    Broker,
    BrokerCancelled,
    BrokerEvent,
    BrokerFill,
    BrokerOrderAck,
    BrokerOrderRequest,
    BrokerReject,
    BrokerUnavailable,
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
        self.positions: dict[str, int] = defaultdict(int)
        self.quotes: dict[str, Quote] = {}
        self.fx: Decimal = Decimal("1400")
        self.orders: dict[str, SimOrder] = {}
        self.pending_events: list[BrokerEvent] = []
        self.reject_next: BrokerReject | None = None
        self.fail_next: bool = False
        self._seq = itertools.count(1)
        self._fill_seq = itertools.count(1)

    # ───────────── 테스트 조작 ─────────────
    def deposit(self, currency: str, amount: Decimal) -> None:
        self.cash[currency] += amount

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
        self._settle(o.req.instrument, o.req.side, qty, price, venue)
        self.pending_events.append(
            BrokerFill(no, f"F{next(self._fill_seq):06d}", o.req.instrument.id, o.req.side, qty, price, venue, self.clock())
        )

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
        self.orders[no] = SimOrder(no, req, req.price, req.qty)
        return BrokerOrderAck(no, "00950")

    async def cancel(self, broker_order_no: str, broker_org_no: str, instrument: Instrument, qty: int) -> None:
        o = self.orders.get(broker_order_no)
        if o is None or not o.open:
            raise BrokerReject("SIM_NOT_OPEN", "취소 가능한 주문 아님")
        canceled, o.remaining, o.open = o.remaining, 0, False
        self.pending_events.append(BrokerCancelled(broker_order_no, canceled))

    async def amend(
        self, broker_order_no: str, broker_org_no: str, instrument: Instrument, price: Decimal, qty: int
    ) -> BrokerOrderAck:
        o = self.orders.get(broker_order_no)
        if o is None or not o.open:
            raise BrokerReject("SIM_NOT_OPEN", "정정 가능한 주문 아님")
        o.remaining, o.open = 0, False
        no = f"S{next(self._seq):07d}"
        req = BrokerOrderRequest(o.req.instrument, o.req.side, "limit", qty, price, o.req.route)
        self.orders[no] = SimOrder(no, req, price, qty)
        return BrokerOrderAck(no, broker_org_no)

    async def quote(self, instrument: Instrument) -> Quote:
        q = self.quotes.get(instrument.id)
        if q is None:
            raise BrokerUnavailable(f"sim: {instrument.id} 시세 없음")
        return q

    async def fx_rate(self, pair: str = "USDKRW") -> Decimal:
        return self.fx
