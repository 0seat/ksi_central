"""주문 동기화와 복구.

브로커의 당일 주문체결 내역과 장부를 비교해 두 가지를 처리한다.
1. UNKNOWN 주문 확정: 접수 응답을 못 받은 주문을 브로커 내역에서 찾아 연결한다.
   - 장부에 연결되지 않은 브로커 주문 중 종목/방향/수량/가격이 같은 것이 정확히 1건이면 연결
   - 없으면 일정 시간 뒤 SUBMIT_FAILED (홀드 해제)
   - 2건 이상이면 자동으로 판단하지 않고 경고 이벤트만 남긴다
2. 누락 보정: 브로커 누적 체결수량이 장부보다 많으면 차이만큼 체결을 만들고,
   잔량 없이 종료된 주문은 취소 처리한다 (체결통보 유실, 재시작 중 체결 등).

실행 시점: 서버 시작 직후, 체결통보 WebSocket 재연결 직후, 그리고 주기적으로.
주의: 체결통보가 지연 도착하는 순간에 동기화가 겹치면 같은 체결이 두 번 반영될 수 있다.
OMS 가 주문 잔량을 넘는 체결은 잘라내지만, 부분체결 구간에서는 중복 가능성이 남는다.
그래서 주기 실행은 체결통보가 정상일 때 수 분 간격으로만 돌린다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable

from central.broker.base import BrokerCancelled, BrokerFill, BrokerOrderStatus
from central.core.events import Event
from central.core.market_hours import KST, now_kst
from central.core.money import ZERO, q_price
from central.oms.orders import OrderService, OrderView

log = logging.getLogger(__name__)

OPEN = ("ACCEPTED", "PARTIALLY_FILLED", "CANCEL_PENDING")
UNKNOWN_TIMEOUT = timedelta(minutes=2)


@dataclass
class SyncReport:
    resolved: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)
    ambiguous: list[int] = field(default_factory=list)
    synthetic_fills: int = 0
    cancels: int = 0


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=KST)


class OrderSync:
    def __init__(
        self, orders: OrderService, *, clock: Callable[[], datetime] = now_kst,
        unknown_timeout: timedelta = UNKNOWN_TIMEOUT,
    ) -> None:
        self.orders = orders
        self.clock = clock
        self.unknown_timeout = unknown_timeout

    async def run(self, broker_alias: str, market: str) -> SyncReport:
        broker = self.orders.brokers[broker_alias]
        now = self.clock()
        statuses = await broker.order_statuses(market, now.date())
        report = SyncReport()
        await self._resolve_unknown(broker_alias, market, statuses, now, report)
        await self._catch_up(broker_alias, market, statuses, now, report)
        if report.resolved or report.failed or report.ambiguous or report.synthetic_fills or report.cancels:
            log.info("[%s/%s] 동기화: %s", broker_alias, market, report)
        return report

    async def _resolve_unknown(
        self, alias: str, market: str, statuses: list[BrokerOrderStatus], now: datetime, report: SyncReport,
    ) -> None:
        known = self.orders.known_broker_order_nos(alias)
        for o in self.orders.orders_in(alias, market, ("UNKNOWN",)):
            candidates = [st for st in statuses if st.broker_order_no not in known and not st.orig_order_no
                          and _matches(o, st)]
            if len(candidates) == 1:
                no = candidates[0].broker_order_no
                await self.orders.resolve_unknown(alias, o.id, no)
                known.add(no)
                report.resolved.append(o.id)
            elif not candidates:
                if now - _aware(o.created_at) >= self.unknown_timeout:
                    await self.orders.fail_unknown(alias, o.id, "브로커 주문내역에 없음 (동기화 확인)")
                    report.failed.append(o.id)
            else:
                report.ambiguous.append(o.id)
                self.orders.bus.publish(Event("ORDER_AMBIGUOUS", o.sub_account_id, {
                    "order_id": o.id, "candidates": [c.broker_order_no for c in candidates]}, now))
                log.error("UNKNOWN 주문 %s 후보 %d건, 수동 확인 필요", o.id, len(candidates))

    async def _catch_up(
        self, alias: str, market: str, statuses: list[BrokerOrderStatus], now: datetime, report: SyncReport,
    ) -> None:
        by_no = {st.broker_order_no: st for st in statuses}
        for o in self.orders.orders_in(alias, market, OPEN):
            st = by_no.get(o.broker_order_no or "")
            if st is None:
                continue
            if st.filled_qty > o.filled_qty:
                diff = st.filled_qty - o.filled_qty
                price = _missing_fill_price(o, st, diff)
                await self.orders.on_broker_event(alias, BrokerFill(
                    st.broker_order_no, f"SYNC-{now:%Y%m%d}-{st.broker_order_no}-{st.filled_qty}",
                    o.symbol, o.side, diff, price, None, now))  # type: ignore[arg-type]
                report.synthetic_fills += 1
            if st.closed_unfilled:
                await self.orders.on_broker_event(alias, BrokerCancelled(st.broker_order_no, st.qty - st.filled_qty))
                report.cancels += 1


def _matches(o: OrderView, st: BrokerOrderStatus) -> bool:
    if st.instrument_id != o.symbol or st.side != o.side or st.qty != o.qty:
        return False
    if o.order_type == "limit":
        return st.price is not None and st.price == o.price
    return True


def _missing_fill_price(o: OrderView, st: BrokerOrderStatus, diff: int) -> Decimal:
    """브로커 누적 평균가와 장부 평균가로 누락분의 가격을 역산한다."""
    if st.avg_fill_price is None:
        return o.price or ZERO
    total = st.avg_fill_price * st.filled_qty
    ours = (o.avg_fill_price or ZERO) * o.filled_qty
    return q_price((total - ours) / diff)
