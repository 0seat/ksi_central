"""중앙증권 클라이언트 SDK (docs/PLAN.md 8.7).

    async with CentralClient("http://100.x.y.z:8700", api_key) as cs:
        await cs.buy("KR:005930", qty=10, price="61000")
        await cs.buy("US:AAPL", qty=3, price="187.25")
        async for msg in cs.stream():
            ...

금액과 가격은 문자열(또는 Decimal)로 주고받는다. 응답의 금액도 문자열이다.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from typing import Any, AsyncIterator

import httpx

API_PREFIX = "/api/v1"


class CentralError(Exception):
    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(f"[{code}] {message}")
        self.code, self.message, self.status = code, message, status


def _price(v: str | Decimal | int | None) -> str | None:
    if v is None:
        return None
    if isinstance(v, float):
        raise TypeError("가격은 문자열이나 Decimal 로 줄 것 (float 금지)")
    return str(v)


class CentralClient:
    def __init__(self, base_url: str, api_key: str, *, timeout: float = 10.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._http = httpx.AsyncClient(base_url=self.base_url + API_PREFIX, headers={"X-API-Key": api_key},
                                       timeout=timeout, transport=transport)

    async def __aenter__(self) -> CentralClient:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()

    async def _req(self, method: str, path: str, **kw) -> Any:
        if "params" in kw:
            kw["params"] = {k: v for k, v in kw["params"].items() if v is not None}
        res = await self._http.request(method, path, **kw)
        try:
            body = res.json()
        except ValueError:
            raise CentralError("INVALID_RESPONSE", res.text[:200], res.status_code) from None
        if not body.get("ok"):
            err = body.get("error", {})
            raise CentralError(err.get("code", "UNKNOWN"), err.get("message", ""), res.status_code)
        return body["data"]

    # ───────────── 계좌 ─────────────
    async def me(self) -> dict:
        return await self._req("GET", "/me")

    async def balance(self) -> dict:
        return await self._req("GET", "/me/balance")

    async def positions(self, market: str | None = None) -> list[dict]:
        return await self._req("GET", "/me/positions", params={"market": market})

    async def ledger(self, **filters) -> list[dict]:
        return await self._req("GET", "/me/ledger", params=_dates(filters))

    async def pnl(self, **filters) -> dict:
        return await self._req("GET", "/me/pnl", params=_dates(filters))

    # ───────────── 주문 ─────────────
    async def place(self, symbol: str, side: str, qty: int, price: str | Decimal | int | None = None, *,
                    order_type: str | None = None, route: str | None = None,
                    client_order_id: str | None = None) -> dict:
        """price 를 생략하면 시장가 (국내만 지원)."""
        body = {
            "client_order_id": client_order_id or f"sdk-{uuid.uuid4().hex[:24]}",
            "symbol": symbol, "side": side, "qty": qty,
            "order_type": order_type or ("market" if price is None else "limit"),
            "price": _price(price), "route": route,
        }
        return await self._req("POST", "/me/orders", json={k: v for k, v in body.items() if v is not None})

    async def buy(self, symbol: str, qty: int, price: str | Decimal | int | None = None, **kw) -> dict:
        return await self.place(symbol, "buy", qty, price, **kw)

    async def sell(self, symbol: str, qty: int, price: str | Decimal | int | None = None, **kw) -> dict:
        return await self.place(symbol, "sell", qty, price, **kw)

    async def cancel(self, order_id: int) -> dict:
        return await self._req("POST", f"/me/orders/{order_id}/cancel")

    async def amend(self, order_id: int, price: str | Decimal | int, qty: int | None = None) -> dict:
        body: dict = {"price": _price(price)}
        if qty is not None:
            body["qty"] = qty
        return await self._req("POST", f"/me/orders/{order_id}/amend", json=body)

    async def cancel_all(self, market: str | None = None) -> dict:
        return await self._req("POST", "/me/orders/cancel-all", params={"market": market})

    async def order(self, order_id: int) -> dict:
        return await self._req("GET", f"/me/orders/{order_id}")

    async def orders(self, **filters) -> list[dict]:
        return await self._req("GET", "/me/orders", params=_dates(filters))

    async def fills(self, **filters) -> list[dict]:
        return await self._req("GET", "/me/fills", params=_dates(filters))

    # ───────────── 시세 ─────────────
    async def quote(self, symbol: str) -> dict:
        return await self._req("GET", f"/market/quote/{symbol}")

    async def market_status(self, market: str | None = None) -> dict:
        return await self._req("GET", "/market/status", params={"market": market})

    async def fx(self) -> dict:
        return await self._req("GET", "/market/fx")

    async def candles(self, symbol: str, interval: str = "1d", *, start=None, end=None, limit: int = 500) -> dict:
        """interval: 1m 5m 15m 30m 60m 1d 1w 1M. start/end 는 date, datetime 또는 ISO 문자열."""
        return await self._req("GET", f"/market/candles/{symbol}", params=_dates(
            {"interval": interval, "limit": limit, "start": start, "end": end}))

    async def history(self, symbol: str, interval: str = "1d", *, adjust: str = "none", start=None, end=None,
                      limit: int = 5000) -> dict:
        """보관소 조회 (KIS 에서 만료된 과거 분봉 포함). symbol 예: KR:005930, US:AAPL, INDEX:KR:0001.
        adjust: none(원주가) / split(분할·병합만) / total(배당까지). interval: 1m, 1d."""
        return await self._req("GET", f"/market/history/{symbol}", params=_dates(
            {"interval": interval, "adjust": adjust, "limit": limit, "start": start, "end": end}))

    async def orderbook(self, symbol: str) -> dict:
        return await self._req("GET", f"/market/orderbook/{symbol}")

    async def ticks(self, symbol: str, day=None, limit: int = 10000) -> dict:
        return await self._req("GET", f"/market/ticks/{symbol}", params={"date": day and str(day), "limit": limit})

    async def calendar(self, market: str = "KR", *, start=None, end=None) -> dict:
        return await self._req("GET", "/market/calendar", params=_dates({"market": market, "start": start, "end": end}))

    async def symbols(self, q: str = "", market: str | None = None, limit: int = 50) -> list[dict]:
        return await self._req("GET", "/market/symbols", params={"q": q, "market": market, "limit": limit})

    # ───────────── 실시간 ─────────────
    async def stream(self, channels: list[str] | None = None) -> AsyncIterator[dict]:
        """orders, fills, account, system 이벤트를 받는다. 끊기면 반복이 끝나므로 호출 측에서 재접속한다."""
        import websockets

        url = self.base_url.replace("http://", "ws://", 1).replace("https://", "wss://", 1) + API_PREFIX + "/ws"
        async with websockets.connect(url, additional_headers={"X-API-Key": self._api_key}) as ws:
            if channels:
                await ws.send(json.dumps({"op": "subscribe", "channels": channels}))
            async for raw in ws:
                yield json.loads(raw)


def _dates(filters: dict) -> dict:
    # from/to 는 파이썬 예약어라 start/end 로 받는다
    out = dict(filters)
    for key, name in (("start", "from"), ("end", "to")):
        if key in out:
            v = out.pop(key)
            if v is not None:
                out[name] = v.isoformat() if hasattr(v, "isoformat") else str(v)
    return out
