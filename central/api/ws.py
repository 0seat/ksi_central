"""실시간 WebSocket (/ws) — docs/PLAN.md 8.5

인증: 접속 요청 헤더 X-API-Key (쿼리스트링은 접근 로그에 남으므로 받지 않는다).
자동 구독: orders, fills, account, system — 자기 서브계좌 이벤트만 받는다.
시세 채널(tick:, orderbook:, candle.*:)은 M5 구독 허브에서 제공한다.

클라이언트 → {"op": "subscribe", "channels": [...]} / {"op": "ping"}
서버 → {"ch": "orders" | "fills" | "account" | "system", "data": {...}}
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from central.api.common import out
from central.core.events import Event
from central.oms.accounts import SubAccount

log = logging.getLogger(__name__)
router = APIRouter()

QUEUE_SIZE = 1000
AUTO_CHANNELS = ("orders", "fills", "account", "system")
_ACCOUNT_EVENTS = {"TRANSFER", "ADJUST", "POSITION_TRANSFER", "POSITION_IMPORT", "SUB_ACCOUNT_STATUS"}
_SYSTEM_EVENTS = {"HALT", "RESUME", "TRADING_ENABLED"}


@dataclass(eq=False)
class _Conn:
    sub: SubAccount
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(QUEUE_SIZE))
    overflow: asyncio.Event = field(default_factory=asyncio.Event)


class EventHub:
    """EventBus → 서브계좌별 WebSocket 연결."""

    def __init__(self) -> None:
        self._conns: set[_Conn] = set()

    def add(self, sub: SubAccount) -> _Conn:
        conn = _Conn(sub)
        self._conns.add(conn)
        return conn

    def remove(self, conn: _Conn) -> None:
        self._conns.discard(conn)

    @property
    def connections(self) -> int:
        return len(self._conns)

    def on_event(self, e: Event) -> None:
        routed = self._route(e)
        if routed is None:
            return
        ch, targets = routed
        msg = {"ch": ch, "data": out({"type": e.type, "ts": e.ts, **e.payload})}
        for conn in list(self._conns):
            if targets(conn.sub):
                try:
                    conn.queue.put_nowait(msg)
                except asyncio.QueueFull:
                    conn.overflow.set()  # 느린 클라이언트: 연결을 끊고 REST 로 다시 맞추게 한다

    @staticmethod
    def _route(e: Event):
        sid = e.sub_account_id
        if e.type in ("ORDER", "ORDER_UNKNOWN"):
            return "orders", lambda s: s.id == sid
        if e.type == "FILL":
            return "fills", lambda s: s.id == sid
        if e.type in _ACCOUNT_EVENTS:
            other = e.payload.get("to")
            return "account", lambda s: s.id == sid or s.id == other
        if e.type in _SYSTEM_EVENTS and sid is None:
            p = e.payload
            if e.type == "TRADING_ENABLED":
                return "system", lambda s: s.broker_account_id == p["broker_account_id"]
            scope, id_ = p["scope"], p["id"]
            return "system", {
                "all": lambda s: True,
                "broker": lambda s: s.broker_account_id == id_,
                "broker_market": lambda s: s.broker_account_id == id_.partition(":")[0],
                "sub": lambda s: str(s.id) == id_,
                "market": lambda s: id_ in s.markets,
            }[scope]
        return None  # 대사 결과 등 실계좌 전체 정보는 서브계좌에 보내지 않는다


@router.websocket("/ws")
async def websocket(ws: WebSocket) -> None:
    app = ws.app.state.central
    hub: EventHub = ws.app.state.hub
    sub = app.apikeys.authenticate(ws.headers.get("x-api-key"))
    if sub is None:
        await ws.close(code=4401, reason="UNAUTHORIZED")
        return
    await ws.accept()
    conn = hub.add(sub)
    await ws.send_json({"ch": "system", "data": {
        "type": "WELCOME", "sub_account_id": sub.id, "channels": list(AUTO_CHANNELS)}})

    async def reader() -> None:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
                op = msg.get("op")
            except (ValueError, AttributeError):
                await conn.queue.put(_error("INVALID_REQUEST", "JSON 객체를 보낼 것"))
                continue
            if op == "ping":
                await conn.queue.put({"ch": "system", "data": {"type": "PONG"}})
            elif op == "subscribe":
                channels = [c for c in msg.get("channels", []) if c not in AUTO_CHANNELS]
                if channels:
                    await conn.queue.put(_error("NOT_IMPLEMENTED", f"시세 채널은 M5 에서 제공 예정: {channels}"))
                else:
                    await conn.queue.put({"ch": "system", "data": {"type": "SUBSCRIBED", "channels": list(AUTO_CHANNELS)}})
            else:
                await conn.queue.put(_error("INVALID_REQUEST", f"알 수 없는 op: {op}"))

    async def writer() -> None:
        while True:
            get = asyncio.ensure_future(conn.queue.get())
            over = asyncio.ensure_future(conn.overflow.wait())
            done, _ = await asyncio.wait({get, over}, return_when=asyncio.FIRST_COMPLETED)
            if over in done:
                get.cancel()
                await ws.close(code=4429, reason="SLOW_CONSUMER")
                return
            over.cancel()
            await ws.send_json(get.result())

    tasks = [asyncio.create_task(reader()), asyncio.create_task(writer())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        hub.remove(conn)
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, Exception):
                await t


def _error(code: str, message: str) -> dict:
    return {"ch": "system", "data": {"type": "ERROR", "code": code, "message": message}}
