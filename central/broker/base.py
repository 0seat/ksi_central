"""브로커 공통 인터페이스. KisBroker(M3)와 SimBroker 가 구현한다.

주문 결과는 두 경로로 온다.
- 동기: submit/cancel/amend 의 반환값 또는 예외 (접수 단계)
- 비동기: 이벤트 핸들러로 전달되는 BrokerFill / BrokerCancelled / BrokerOrderRejected (체결통보 단계)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Awaitable, Callable, Literal

from central.core.instruments import Instrument

Side = Literal["buy", "sell"]
OrderType = Literal["limit", "market"]


class BrokerReject(Exception):
    """브로커가 명확히 거부함 (주문이 접수되지 않음)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


class BrokerUnavailable(Exception):
    """결과를 알 수 없음 (타임아웃, 연결 오류). 주문이 접수됐을 수도 있다."""


@dataclass(frozen=True)
class BrokerOrderRequest:
    instrument: Instrument
    side: Side
    order_type: OrderType
    qty: int
    price: Decimal | None
    route: str | None = None


@dataclass(frozen=True)
class BrokerOrderAck:
    broker_order_no: str
    broker_org_no: str = ""


@dataclass(frozen=True)
class Quote:
    last: Decimal
    upper_limit: Decimal | None = None
    lower_limit: Decimal | None = None


@dataclass(frozen=True)
class BrokerFill:
    broker_order_no: str
    fill_key: str          # 체결 고유키 (중복 통보 제거용)
    instrument_id: str
    side: Side
    qty: int
    price: Decimal
    venue: str | None
    filled_at: datetime


@dataclass(frozen=True)
class BrokerCancelled:
    broker_order_no: str
    qty: int  # 취소된 잔량


@dataclass(frozen=True)
class BrokerOrderRejected:
    broker_order_no: str
    reason: str


BrokerEvent = BrokerFill | BrokerCancelled | BrokerOrderRejected
EventHandler = Callable[[BrokerEvent], Awaitable[None]]


class Broker(ABC):
    account_id: str

    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        self._handler: EventHandler | None = None

    def set_event_handler(self, handler: EventHandler) -> None:
        self._handler = handler

    async def emit(self, event: BrokerEvent) -> None:
        if self._handler is not None:
            await self._handler(event)

    @abstractmethod
    async def submit(self, req: BrokerOrderRequest) -> BrokerOrderAck: ...

    @abstractmethod
    async def cancel(self, broker_order_no: str, broker_org_no: str, instrument: Instrument, qty: int) -> None: ...

    @abstractmethod
    async def amend(
        self, broker_order_no: str, broker_org_no: str, instrument: Instrument, price: Decimal, qty: int
    ) -> BrokerOrderAck: ...

    @abstractmethod
    async def quote(self, instrument: Instrument) -> Quote: ...

    @abstractmethod
    async def fx_rate(self, pair: str = "USDKRW") -> Decimal: ...
