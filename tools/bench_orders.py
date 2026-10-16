"""주문 처리 속도 벤치마크: 실제 HTTP 서버 + 파일 DB + KIS 를 흉내 내는 가짜 브로커.

KIS 에는 주문을 내지 않는다. 가짜 브로커는 KIS 와 같은 계좌별 호출 간격(RateLimiter)과
설정한 응답 지연을 두고, 시장가 주문은 접수 직후 체결 통보를 보낸다.

    python -m tools.bench_orders                 # 기본 시나리오 전부
    python -m tools.bench_orders --orders 400 --clients 8
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket
import statistics
import tempfile
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import httpx
import uvicorn

from central.api.server import create_app
from central.app import CentralApp
from central.broker.base import BrokerOrderAck, BrokerOrderRequest, Quote
from central.broker.kis.rest import RateLimiter
from central.broker.sim import SimBroker
from central.client import CentralClient
from central.core.instruments import Instrument
from central.settings import load_settings
from central.storage.db import Database
from tests.conftest import write_home
from tests.oms_env import KR_OPEN, Clock

ADMIN = "dummy-admin-key-0000000000"
SYMBOL = "KR:005930"
PRICE = Decimal("61000")


class FakeKis(SimBroker):
    """KIS 호출(시세, 주문)마다 계좌 RateLimiter 를 거치고 latency 만큼 기다린다. 시장가는 즉시 체결."""

    def __init__(self, *, rate: float | None, latency_ms: float, fill_ms: float, clock) -> None:
        super().__init__("real", integrated_margin=True, clock=clock)
        self.limiter = RateLimiter(rate) if rate else None
        self.latency = latency_ms / 1000
        self.fill_delay = fill_ms / 1000
        self.fill_handle_s: list[float] = []

    async def _call(self) -> None:
        if self.limiter:
            await self.limiter.acquire()
        if self.latency:
            await asyncio.sleep(self.latency)

    async def quote(self, instrument: Instrument) -> Quote:
        await self._call()
        return await super().quote(instrument)

    async def submit(self, req: BrokerOrderRequest) -> BrokerOrderAck:
        await self._call()
        ack = await super().submit(req)
        if req.order_type == "market":
            asyncio.get_running_loop().create_task(self._fill_later(ack.broker_order_no, req.qty))
        return ack

    async def _fill_later(self, no: str, qty: int) -> None:
        await asyncio.sleep(self.fill_delay)
        self.fill(no, qty, PRICE)
        t = time.perf_counter()
        await self.drain()                       # 체결 통보 처리 (DB 반영, 이벤트 발행)
        self.fill_handle_s.append(time.perf_counter() - t)


@dataclass
class Result:
    name: str
    n: int
    wall: float
    lat: list[float]
    fill: list[float]
    fill_wait: float

    def line(self) -> str:
        lat = sorted(self.lat)
        p = lambda q: lat[min(len(lat) - 1, int(len(lat) * q))] * 1000
        fh = statistics.median(self.fill) * 1000 if self.fill else 0
        return (f"{self.name:<34} {self.n:>5}건 {self.n / self.wall:>7.1f}건/초  "
                f"응답 p50 {p(0.5):>7.1f} p95 {p(0.95):>7.1f} max {lat[-1] * 1000:>7.1f} ms  "
                f"체결처리 p50 {fh:.1f} ms  전부 체결까지 {self.fill_wait:.2f}s")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def scenario(name: str, *, orders: int, clients: int, rate: float | None, latency_ms: float,
                   fill_ms: float) -> Result:
    with tempfile.TemporaryDirectory() as tmp:
        home = write_home(Path(tmp))
        clock = Clock(KR_OPEN)
        broker = FakeKis(rate=rate, latency_ms=latency_ms, fill_ms=fill_ms, clock=clock)
        broker.set_quote(SYMBOL, PRICE, upper=Decimal("79300"), lower=Decimal("42700"))
        paper = SimBroker("paper", clock=clock)
        app = CentralApp(load_settings(home), db=Database.file(Path(tmp) / "central.db"),
                         brokers={"real": broker, "paper": paper}, clock=clock)
        port = free_port()
        server = uvicorn.Server(uvicorn.Config(create_app(app, manage_lifecycle=False), host="127.0.0.1",
                                               port=port, log_level="warning", access_log=False))
        serve = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.01)
        base = f"http://127.0.0.1:{port}"
        try:
            broker.deposit("KRW", Decimal("10000000000"))
            app.accounts.deposit_unallocated("real", "KRW", Decimal("10000000000"))
            keys = []
            async with httpx.AsyncClient(base_url=base + "/api/v1", headers={"X-Admin-Key": ADMIN}) as adm:
                async def post(path: str, body: dict | None = None, method: str = "POST") -> dict:
                    res = (await adm.request(method, path, json=body)).json()
                    assert res["ok"], res
                    return res["data"]
                await post("/admin/broker-accounts/real", {"trading_enabled": True}, "PATCH")
                un = app.accounts.unallocated("real").id
                for i in range(clients):
                    sub = await post("/admin/sub-accounts", {"broker_account_id": "real", "name": f"B{i}",
                                                             "markets": ["KR"]})
                    await post("/admin/transfers", {"from": un, "to": sub["id"], "currency": "KRW",
                                                    "amount": "1000000000"})
                    keys.append((await post(f"/admin/sub-accounts/{sub['id']}/api-keys", {"label": "bench"}))["api_key"])

            per = orders // clients
            lat: list[float] = []

            clients_ = [CentralClient(base, k) for k in keys]
            for cs in clients_:                      # 매도할 수량을 먼저 사 둔다 (측정 제외)
                await cs.__aenter__()
                o = await cs.place(SYMBOL, "buy", per)
                while (await cs.order(o["id"]))["status"] != "FILLED":
                    await asyncio.sleep(0.01)
            broker.fill_handle_s.clear()

            async def run(cs: CentralClient) -> None:
                for j in range(per):
                    t = time.perf_counter()
                    o = await cs.place(SYMBOL, "sell" if j % 2 == 0 else "buy", 1)
                    lat.append(time.perf_counter() - t)
                    assert o["status"] in ("ACCEPTED", "FILLED", "PARTIALLY_FILLED"), o

            t0 = time.perf_counter()
            await asyncio.gather(*(run(cs) for cs in clients_))
            wall = time.perf_counter() - t0
            t1 = time.perf_counter()
            while len(broker.fill_handle_s) < per * clients:
                await asyncio.sleep(0.005)
            for cs in clients_:
                await cs.__aexit__(None, None, None)
            return Result(name, per * clients, wall, lat, broker.fill_handle_s, time.perf_counter() - t1)
        finally:
            server.should_exit = True
            await serve
            await app.http.aclose()
            app.db.engine.dispose()


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--orders", type=int, default=200)
    ap.add_argument("--clients", type=int, default=8)
    ap.add_argument("--kis-orders", type=int, default=12)
    args = ap.parse_args()
    logging.basicConfig(level=logging.ERROR)

    runs = [
        ("서버 단독, 앱 1개 순차", dict(orders=args.orders, clients=1, rate=None, latency_ms=0, fill_ms=0)),
        (f"서버 단독, 앱 {args.clients}개 동시", dict(orders=args.orders, clients=args.clients, rate=None,
                                                 latency_ms=0, fill_ms=0)),
        ("KIS 지연 70ms, 한도 없음", dict(orders=60, clients=4, rate=None, latency_ms=70, fill_ms=300)),
        ("모의 조건 (1건/초, 70ms)", dict(orders=args.kis_orders, clients=2, rate=1, latency_ms=70, fill_ms=300)),
        ("실전 조건 (2건/초, 70ms)", dict(orders=args.kis_orders, clients=2, rate=2, latency_ms=70, fill_ms=300)),
        ("공식 한도 (20건/초, 70ms)", dict(orders=60, clients=4, rate=20, latency_ms=70, fill_ms=300)),
    ]
    for name, kw in runs:
        print((await scenario(name, **kw)).line(), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
