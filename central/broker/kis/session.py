"""계좌(앱키)당 하나뿐인 KIS 실시간 WebSocket 세션.

실측(2026-10-14): 같은 앱키로 두 번째 연결을 열면 OPSP8996 "ALREADY IN USE appkey" 로 거부된다.
그래서 체결통보와 시세 등록이 연결 하나를 공유한다.

- 원하는 등록 목록(desired)을 들고 있다가 연결될 때마다 전부 다시 등록한다 (재연결 = 재구독)
- 체결통보(kind="notice")를 먼저 등록하고, 실패하면 연결을 다시 시도한다
- 시세(kind="data") 등록이 거부되면 목록에서 빼고 on_dropped 로 알린다
- 등록 한도는 profile.ws_max_subscriptions (공식 41건, 계좌별 실측값으로 설정)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from central.broker.kis.auth import KisAuth
from central.broker.kis.ws import KisWebSocket, KisWsError, RealtimeMessage

log = logging.getLogger(__name__)

Handler = Callable[[RealtimeMessage], Awaitable[None]]
_ALREADY = "ALREADY IN SUBSCRIBE"


class SubscriptionLimit(Exception):
    """등록 한도 초과 (로컬 한도 또는 KIS OPSP0008)."""


class KisWsSession:
    def __init__(self, auth: KisAuth, *, ws_factory: Callable[[KisAuth], KisWebSocket] = KisWebSocket,
                 max_backoff: float = 60.0) -> None:
        self.auth = auth
        self.alias = auth.account.alias
        self._factory = ws_factory
        self._max_backoff = max_backoff
        self._ws: KisWebSocket | None = None
        self._desired: dict[tuple[str, str], str] = {}
        self._handlers: dict[str, Handler] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self.connected = asyncio.Event()
        self.on_reconnect: list[Callable[[], Awaitable[None]]] = []
        self.on_dropped: Callable[[str, str, str], None] | None = None
        self.connects = 0
        self.last_error: str | None = None

    # ───────────── 상태 ─────────────
    @property
    def limit(self) -> int:
        return self.auth.account.profile.ws_max_subscriptions

    @property
    def used(self) -> int:
        return len(self._desired)

    @property
    def free_slots(self) -> int:
        return self.limit - self.used

    def data_subscriptions(self) -> list[tuple[str, str]]:
        """시세 등록 목록 (체결통보는 tr_key 가 HTS ID 라 제외)."""
        return sorted(k for k, kind in self._desired.items() if kind == "data")

    def health(self) -> dict:
        return {"connected": self.connected.is_set(), "connects": self.connects, "slots_used": self.used,
                "slots_limit": self.limit, "data": [f"{t}:{k}" for t, k in self.data_subscriptions()],
                "last_error": self.last_error}

    # ───────────── 등록 ─────────────
    def set_handler(self, tr_id: str, handler: Handler) -> None:
        self._handlers[tr_id] = handler

    def desire(self, tr_id: str, tr_key: str, kind: str = "data") -> None:
        """연결 전에 등록 목록만 채운다 (체결통보 등). 연결되면 등록된다."""
        self._desired[(tr_id, tr_key)] = kind

    async def add(self, tr_id: str, tr_key: str, kind: str = "data") -> None:
        key = (tr_id, tr_key)
        async with self._lock:
            if key in self._desired:
                return
            if self.used >= self.limit:
                raise SubscriptionLimit(f"[{self.alias}] 실시간 등록 한도 {self.limit}건 사용 중")
            self._desired[key] = kind
            ws = self._ws if self.connected.is_set() else None
            if ws is None:
                return  # 연결되면 등록
            try:
                ack = await ws.subscribe(tr_id, tr_key)
            except Exception as e:  # 연결 문제: 목록에 남겨 두고 재연결 때 등록
                log.warning("[%s] 실시간 등록 지연 %s:%s (%s)", self.alias, tr_id, tr_key, e)
                return
            if not ack.ok and _ALREADY not in ack.msg:
                del self._desired[key]
                if ack.msg_cd == "OPSP0008":
                    raise SubscriptionLimit(f"[{self.alias}] KIS 등록 한도 초과 [{ack.msg_cd}] {ack.msg}")
                raise KisWsError(f"[{self.alias}] 실시간 등록 실패 {tr_id}:{tr_key} [{ack.msg_cd}] {ack.msg}")

    async def remove(self, tr_id: str, tr_key: str) -> None:
        async with self._lock:
            if self._desired.pop((tr_id, tr_key), None) is None:
                return
            ws = self._ws if self.connected.is_set() else None
            if ws is not None:
                try:
                    await ws.unsubscribe(tr_id, tr_key)
                except Exception as e:
                    log.warning("[%s] 실시간 해제 실패 %s:%s (%s)", self.alias, tr_id, tr_key, e)

    # ───────────── 연결 루프 ─────────────
    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name=f"kis-ws-session-{self.alias}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        backoff = min(1.0, self._max_backoff)
        while True:
            ws = self._factory(self.auth)
            try:
                await ws.connect()
                async with self._lock:
                    await self._subscribe_all(ws)
                    self._ws = ws
                    self.connected.set()
                self.connects += 1
                self.last_error = None
                backoff = min(1.0, self._max_backoff)
                log.info("[%s] 실시간 세션 연결 (%d회차, 등록 %d/%d)", self.alias, self.connects, self.used, self.limit)
                if self.connects > 1:
                    for cb in self.on_reconnect:
                        try:
                            await cb()
                        except Exception:
                            log.exception("[%s] 재연결 후 처리 실패", self.alias)
                await self._consume(ws)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                log.exception("[%s] 실시간 세션 오류", self.alias)
            finally:
                self.connected.clear()
                self._ws = None
                await ws.close()
            log.warning("[%s] 실시간 세션 재연결 대기 %.0f초", self.alias, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._max_backoff)

    async def _subscribe_all(self, ws: KisWebSocket) -> None:
        for (tr_id, tr_key), kind in sorted(self._desired.items(), key=lambda kv: kv[1] != "notice"):
            ack = await ws.subscribe(tr_id, tr_key)
            if ack.ok or _ALREADY in ack.msg:
                continue
            if kind == "notice":
                raise KisWsError(f"체결통보 등록 실패 {tr_id}: [{ack.msg_cd}] {ack.msg}")
            del self._desired[(tr_id, tr_key)]
            reason = f"[{ack.msg_cd}] {ack.msg}"
            log.warning("[%s] 재연결 후 시세 등록 실패로 제외 %s:%s %s", self.alias, tr_id, tr_key, reason)
            if self.on_dropped:
                self.on_dropped(tr_id, tr_key, reason)

    async def _consume(self, ws: KisWebSocket) -> None:
        # 메시지와 연결 종료를 함께 기다려 끊김을 바로 감지한다
        closed = asyncio.ensure_future(ws.closed.wait())
        try:
            while True:
                get = asyncio.ensure_future(ws.queue.get())
                done, _ = await asyncio.wait({get, closed}, return_when=asyncio.FIRST_COMPLETED)
                if get in done:
                    await self.dispatch(get.result())
                    continue
                get.cancel()
                return
        finally:
            closed.cancel()

    async def dispatch(self, msg: RealtimeMessage) -> None:
        handler = self._handlers.get(msg.tr_id)
        if handler is None:
            return
        try:
            await handler(msg)
        except Exception:
            log.exception("[%s] 실시간 메시지 처리 실패 %s", self.alias, msg.tr_id)
