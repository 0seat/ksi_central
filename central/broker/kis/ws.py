"""KIS 실시간 WebSocket 클라이언트.

메시지 형식 (공식 샘플 kis_auth.KISWebSocket 기준)
- 실시간 데이터: "<암호화 0|1>|<tr_id>|<건수>|<필드^필드^...>"  (여러 건이면 필드가 이어 붙음)
- 시스템 메시지(JSON): 구독 응답(body.rt_cd, body.msg1, body.output.iv/key), PINGPONG
구독 요청: {"header": {approval_key, custtype, tr_type, content-type}, "body": {"input": {tr_id, tr_key}}}
tr_type: "1" 등록, "2" 해제.

자동 재연결/재구독은 Market Data Service(M5)에서 이 클래스를 감싸서 처리한다.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field

import websockets

from central.broker.kis.auth import KisAuth
from central.broker.kis.crypto import aes_cbc_base64_decrypt
from central.broker.kis.tr import WS_BASE, WS_PATH

log = logging.getLogger(__name__)

class KisWsError(Exception):
    pass


@dataclass(frozen=True)
class SubscribeAck:
    tr_id: str
    tr_key: str
    ok: bool
    msg_cd: str
    msg: str
    encrypted: bool


@dataclass
class RealtimeMessage:
    tr_id: str
    encrypted: bool
    records: list[list[str]]


@dataclass
class _Cipher:
    key: str
    iv: str


@dataclass
class KisWebSocket:
    auth: KisAuth
    _ws: websockets.ClientConnection | None = None
    _approval_key: str = ""
    _reader: asyncio.Task | None = None
    _pending: dict[tuple[str, str], asyncio.Future] = field(default_factory=dict)
    _ciphers: dict[str, _Cipher] = field(default_factory=dict)
    _subscriptions: set[tuple[str, str]] = field(default_factory=set)
    queue: asyncio.Queue[RealtimeMessage] = field(default_factory=asyncio.Queue)
    pingpong_count: int = 0

    async def connect(self) -> None:
        self._approval_key = await self.auth.approval_key()
        url = f"{WS_BASE[self.auth.account.profile.env]}{WS_PATH}"
        self._ws = await websockets.connect(url, ping_interval=None)
        self._reader = asyncio.create_task(self._read_loop(), name=f"kis-ws-{self.auth.account.alias}")

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
        if self._ws:
            await self._ws.close()
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()

    async def subscribe(self, tr_id: str, tr_key: str, timeout: float = 10.0) -> SubscribeAck:
        limit = self.auth.account.profile.ws_max_subscriptions
        if len(self._subscriptions) >= limit:
            raise KisWsError(f"구독 한도({limit}) 초과")
        ack = await self._send(tr_id, tr_key, "1", timeout)
        if ack.ok:
            self._subscriptions.add((tr_id, tr_key))
        return ack

    @property
    def free_slots(self) -> int:
        return self.auth.account.profile.ws_max_subscriptions - len(self._subscriptions)

    async def unsubscribe(self, tr_id: str, tr_key: str, timeout: float = 10.0) -> SubscribeAck:
        ack = await self._send(tr_id, tr_key, "2", timeout)
        self._subscriptions.discard((tr_id, tr_key))
        return ack

    async def _send(self, tr_id: str, tr_key: str, tr_type: str, timeout: float) -> SubscribeAck:
        if self._ws is None:
            raise KisWsError("연결되지 않음")
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[(tr_id, tr_key)] = fut
        msg = {
            "header": {
                "approval_key": self._approval_key,
                "custtype": "P",
                "tr_type": tr_type,
                "content-type": "utf-8",
            },
            "body": {"input": {"tr_id": tr_id, "tr_key": tr_key}},
        }
        await self._ws.send(json.dumps(msg))
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop((tr_id, tr_key), None)

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                if raw[:1] in ("0", "1"):
                    self._on_data(raw)
                else:
                    await self._on_system(raw)
        except websockets.ConnectionClosed as e:
            log.warning("[%s] WS 연결 종료: %s", self.auth.account.alias, e)
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(KisWsError("WS 연결 종료"))

    def _on_data(self, raw: str) -> None:
        parts = raw.split("|", 3)
        if len(parts) < 4:
            log.warning("알 수 없는 실시간 메시지 형식")
            return
        flag, tr_id, count_s, payload = parts
        encrypted = flag == "1"
        if encrypted:
            cipher = self._ciphers.get(tr_id)
            if cipher is None:
                log.warning("%s 복호화 키 없음, 메시지 폐기", tr_id)
                return
            payload = aes_cbc_base64_decrypt(cipher.key, cipher.iv, payload)
        fields = payload.split("^")
        count = max(int(count_s), 1)
        per = len(fields) // count
        records = [fields[i * per:(i + 1) * per] for i in range(count)] if per else [fields]
        self.queue.put_nowait(RealtimeMessage(tr_id, encrypted, records))

    async def _on_system(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            log.warning("JSON 아닌 시스템 메시지 수신")
            return
        header = msg.get("header", {})
        tr_id = header.get("tr_id", "")
        if tr_id == "PINGPONG":
            self.pingpong_count += 1
            assert self._ws is not None
            await self._ws.pong(raw.encode("utf-8"))
            return

        body = msg.get("body") or {}
        output = body.get("output") or {}
        if output.get("key") and output.get("iv"):
            self._ciphers[tr_id] = _Cipher(output["key"], output["iv"])
        ack = SubscribeAck(
            tr_id=tr_id,
            tr_key=header.get("tr_key", ""),
            ok=str(body.get("rt_cd", "")) == "0",
            msg_cd=str(body.get("msg_cd", "")),
            msg=str(body.get("msg1", "")).strip(),
            encrypted=header.get("encrypt") == "Y",
        )
        fut = self._pending.get((ack.tr_id, ack.tr_key))
        if fut is None:
            # 응답 header 의 tr_key 가 요청과 다르게 오는 경우 대비: 같은 tr_id 대기건이 하나면 매칭
            same_tr = [f for (t, _), f in self._pending.items() if t == ack.tr_id]
            fut = same_tr[0] if len(same_tr) == 1 else None
        if fut and not fut.done():
            fut.set_result(ack)
        else:
            log.info("WS 시스템 메시지 %s %s: %s", ack.tr_id, ack.msg_cd, ack.msg)
