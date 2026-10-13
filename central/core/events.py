"""내부 이벤트 버스. API 서버(M4)는 이 버스를 구독해 서브계좌별 WebSocket 으로 전달한다."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from central.core.market_hours import now_kst

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Event:
    type: str
    sub_account_id: int | None
    payload: dict[str, Any]
    ts: datetime = field(default_factory=now_kst)


Handler = Callable[[Event], None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: list[Handler] = []

    def subscribe(self, handler: Handler) -> None:
        self._handlers.append(handler)

    def publish(self, event: Event) -> None:
        for handler in self._handlers:
            try:
                handler(event)
            except Exception:
                log.exception("이벤트 핸들러 오류: %s", event.type)
