"""실시간 WebSocket (/ws) — docs/PLAN.md 8.5

인증: 접속 요청 헤더 X-API-Key (쿼리스트링은 접근 로그에 남으므로 받지 않는다).
자동 구독: orders, fills, account, system — 자기 서브계좌 이벤트만 받는다.
시세 채널: tick:KR:005930, orderbook:US:AAPL — 시세 허브(central/marketdata/hub.py)에 참조를 걸고,
연결이 끊기면 자동으로 해제한다.

클라이언트 → {"op": "subscribe" | "unsubscribe", "channels": [...]} / {"op": "ping"}
서버 → {"ch": "orders" | "fills" | "account" | "system" | "tick:..." | "orderbook:...", "data": {...}}
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from central.api.common import out
from central.core.events import Event
from central.marketdata.hub import parse_channel
from central.oms.accounts import SubAccount
from central.oms.errors import OmsError

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
    channels: set[str] = field(default_factory=set)   # 시세 채널
    connected_at: float = field(default_factory=time.time)


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

    def snapshot(self) -> dict[int, dict]:
        """서브계좌별 WebSocket 연결 수, 시세 채널, 가장 오래된 연결 시각."""
        out: dict[int, dict] = {}
        for c in list(self._conns):
            a = out.setdefault(c.sub.id, {"connections": 0, "channels": set(), "since": c.connected_at})
            a["connections"] += 1
            a["channels"] |= c.channels
            a["since"] = min(a["since"], c.connected_at)
        return {k: {**v, "channels": sorted(v["channels"])} for k, v in out.items()}

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

    def on_market(self, channel: str, item: object) -> None:
        """MarketDataHub 리스너: 해당 채널을 구독한 연결에만 보낸다."""
        if channel.startswith("dropped:"):
            channel = channel.removeprefix("dropped:")
            msg = _error("SUBSCRIPTION_DROPPED", f"{channel}: {item}")
            for conn in list(self._conns):
                if channel in conn.channels:
                    conn.channels.discard(channel)
                    self._put(conn, msg)
            return
        msg = None
        for conn in list(self._conns):
            if channel in conn.channels:
                msg = msg or {"ch": channel, "data": out(item)}
                self._put(conn, msg)

    @staticmethod
    def _put(conn: _Conn, msg: dict) -> None:
        try:
            conn.queue.put_nowait(msg)
        except asyncio.QueueFull:
            conn.overflow.set()

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
    market = app.market

    def market_name(c: str) -> str:
        kind, symbol = parse_channel(c)
        return f"{kind}:{app.catalog.resolve(symbol).id}"

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
            elif op in ("subscribe", "unsubscribe"):
                channels = msg.get("channels", [])
                if not isinstance(channels, list) or not all(isinstance(c, str) for c in channels):
                    await conn.queue.put(_error("INVALID_REQUEST", "channels 는 문자열 목록"))
                    continue
                for c in channels:
                    if c in AUTO_CHANNELS:
                        continue
                    try:
                        if op == "subscribe":
                            name = await market.acquire(c)
                            if name in conn.channels:
                                await market.release(name)  # 같은 연결의 중복 구독은 한 번만 센다
                            conn.channels.add(name)
                        else:
                            name = market_name(c)
                            if name in conn.channels:
                                conn.channels.discard(name)
                                await market.release(name)
                    except OmsError as e:
                        await conn.queue.put(_error(e.code, f"{c}: {e.message}"))
                await conn.queue.put({"ch": "system", "data": {
                    "type": "SUBSCRIBED", "channels": list(AUTO_CHANNELS) + sorted(conn.channels)}})
            else:
                await conn.queue.put(_error("INVALID_REQUEST", f"알 수 없는 op: {op}"))

    async def writer() -> None:
        while True:
            get = asyncio.ensure_future(conn.queue.get())
            over = asyncio.ensure_future(conn.overflow.wait())
            try:
                done, _ = await asyncio.wait({get, over}, return_when=asyncio.FIRST_COMPLETED)
            finally:   # 연결 종료로 writer 가 취소되면 대기 중인 두 작업도 정리 (Task was destroyed 방지)
                if not get.done() and not over.done():
                    get.cancel()
                    over.cancel()
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
        for name in list(conn.channels):
            with contextlib.suppress(Exception):
                await market.release(name)
        conn.channels.clear()
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, Exception):
                await t


def _error(code: str, message: str) -> dict:
    return {"ch": "system", "data": {"type": "ERROR", "code": code, "message": message}}
