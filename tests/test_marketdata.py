"""M5-A: 실시간 세션 공유, 시세 허브, 파서, 틱 저장, API 시세 채널."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from central.api.server import create_app
from central.app import CentralApp
from central.broker.kis.auth import KisAuth
from central.broker.kis.session import KisWsSession, SubscriptionLimit
from central.broker.kis.ws import RealtimeMessage, SubscribeAck
from central.broker.sim import SimBroker
from central.core.instruments import Instrument, InstrumentRegistry
from central.core.market_hours import KST
from central.marketdata import realtime as rt
from central.marketdata.hub import MarketDataHub
from central.marketdata.ticks import TickStore
from central.oms.catalog import InstrumentCatalog
from central.oms.errors import OmsError
from central.settings import MarketDataConfig, load_settings
from central.storage.db import Database
from tests.oms_env import KR_OPEN, Clock

ADMIN = {"X-Admin-Key": "dummy-admin-key-0000000000"}
TODAY = date(2026, 10, 14)


def kr_tick(code: str = "005930", price: str = "61000", qty: str = "10", hhmmss: str = "100001") -> list[str]:
    rec = [""] * len(rt.KR_TICK_COLUMNS)
    vals = {"MKSC_SHRN_ISCD": code, "STCK_CNTG_HOUR": hhmmss, "STCK_PRPR": price, "CNTG_VOL": qty,
            "ACML_VOL": "1000", "ASKP1": str(int(price) + 100), "BIDP1": price, "CCLD_DVSN": "1",
            "BSOP_DATE": "20261014"}
    for k, v in vals.items():
        rec[rt.KR_TICK_COLUMNS.index(k)] = v
    return rec


def us_tick(with_rsym: bool = True) -> list[str]:
    rec = [""] * len(rt.US_TICK_COLUMNS)
    vals = {"SYMB": "AAPL", "KYMD": "20261014", "KHMS": "233001", "LAST": "187.2500", "EVOL": "5",
            "TVOL": "123456", "PBID": "187.2400", "PASK": "187.2600"}
    for k, v in vals.items():
        rec[rt.US_TICK_COLUMNS.index(k)] = v
    return (["DNASAAPL"] + rec) if with_rsym else rec


# ───────────── 파서 ─────────────
def test_parse_kr_tick() -> None:
    t = rt.parse_kr_tick("H0UNCNT0", kr_tick(), TODAY)
    assert t is not None
    assert (t.symbol, t.price, t.qty, t.cum_volume, t.bid, t.ask, t.side, t.venue) == (
        "KR:005930", D("61000"), 10, 1000, D("61000"), D("61100"), "buy", "UNIFIED")
    assert t.ts == datetime(2026, 10, 14, 10, 0, 1, tzinfo=KST)
    assert rt.parse_kr_tick("H0STCNT0", kr_tick(), TODAY).venue == "KRX"  # type: ignore[union-attr]


@pytest.mark.parametrize("with_rsym", [True, False])
def test_parse_us_tick_with_or_without_rsym(with_rsym: bool) -> None:
    t = rt.parse_us_tick(us_tick(with_rsym), TODAY)
    assert t is not None
    assert (t.symbol, t.price, t.qty, t.cum_volume, t.bid, t.ask) == (
        "US:AAPL", D("187.2500"), 5, 123456, D("187.2400"), D("187.2600"))
    assert t.ts == datetime(2026, 10, 14, 23, 30, 1, tzinfo=KST)


def test_parse_kr_book() -> None:
    rec = ["005930", "100001", "0"] + [str(61100 + i * 100) for i in range(10)] + \
          [str(61000 - i * 100) for i in range(10)] + [str(i + 1) for i in range(10)] + \
          [str(i + 11) for i in range(10)] + ["0"] * 20
    b = rt.parse_kr_book("H0UNASP0", rec, TODAY)
    assert b is not None and b.asks[0] == (D("61100"), 1) and b.bids[0] == (D("61000"), 11) and len(b.asks) == 10


def test_parse_us_book_71_fields() -> None:
    header = ["AAPL", "4", "20261014", "103001", "20261014", "233001", "100", "200", "0", "0"]
    level1 = ["187.24", "187.26", "300", "400", "0", "0"]
    rec = ["DNASAAPL"] + header + level1 + ["0"] * 54
    assert len(rec) == 71
    b = rt.parse_us_book(rec, TODAY)
    assert b is not None and b.bids == [(D("187.24"), 300)] and b.asks == [(D("187.26"), 400)]


# ───────────── 공유 세션 ─────────────
class FakeWs:
    """KisWebSocket 대역: 등록 결과를 스크립트로 정한다."""
    instances: list[FakeWs] = []
    reject: dict[tuple[str, str], str] = {}

    def __init__(self, auth) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()
        self.closed = asyncio.Event()
        self.subscribed: list[tuple[str, str]] = []
        FakeWs.instances.append(self)

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        self.closed.set()

    async def subscribe(self, tr_id: str, tr_key: str) -> SubscribeAck:
        code = FakeWs.reject.get((tr_id, tr_key))
        if code:
            return SubscribeAck(tr_id, tr_key, False, code, "MAX SUBSCRIBE OVER", False)
        self.subscribed.append((tr_id, tr_key))
        return SubscribeAck(tr_id, tr_key, True, "OPSP0000", "SUBSCRIBE SUCCESS", False)

    async def unsubscribe(self, tr_id: str, tr_key: str) -> SubscribeAck:
        self.subscribed.remove((tr_id, tr_key))
        return SubscribeAck(tr_id, tr_key, True, "OPSP0000", "UNSUBSCRIBE SUCCESS", False)


async def _wait(cond, timeout: float = 2.0) -> None:
    end = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("시간 초과")
        await asyncio.sleep(0.01)


async def test_session_shares_connection_and_resubscribes(home: Path) -> None:
    FakeWs.instances, FakeWs.reject = [], {}
    settings = load_settings(home)
    acc = settings.accounts["real"]
    acc = acc.model_copy(update={"profile": acc.profile.model_copy(update={"ws_max_subscriptions": 3})})
    async with httpx.AsyncClient() as http:
        session = KisWsSession(KisAuth(acc, settings.tokens_dir, http), ws_factory=FakeWs, max_backoff=0.01)
        reconnects: list[int] = []

        async def on_reconnect() -> None:
            reconnects.append(1)

        session.on_reconnect.append(on_reconnect)
        session.desire("H0STCNI0", "hts", "notice")
        session.desire("H0GSCNI0", "hts", "notice")
        await session.start()
        await _wait(session.connected.is_set)
        await session.add("H0UNCNT0", "005930")
        with pytest.raises(SubscriptionLimit):
            await session.add("H0UNCNT0", "000660")  # 로컬 한도 3
        assert FakeWs.instances[0].subscribed == [("H0STCNI0", "hts"), ("H0GSCNI0", "hts"), ("H0UNCNT0", "005930")]

        # 연결이 끊기면 새 연결에서 체결통보부터 전부 다시 등록하고 재연결 콜백을 부른다
        FakeWs.instances[0].closed.set()
        await _wait(lambda: len(FakeWs.instances) == 2 and session.connected.is_set())
        await _wait(lambda: reconnects == [1])
        assert FakeWs.instances[1].subscribed[:2] == [("H0STCNI0", "hts"), ("H0GSCNI0", "hts")]
        assert ("H0UNCNT0", "005930") in FakeWs.instances[1].subscribed

        # 재연결 때 KIS 가 시세 등록을 거부하면 목록에서 빼고 알린다
        dropped: list = []
        session.on_dropped = lambda t, k, r: dropped.append((t, k))
        FakeWs.reject = {("H0UNCNT0", "005930"): "OPSP0008"}
        FakeWs.instances[1].closed.set()
        await _wait(lambda: len(FakeWs.instances) == 3 and session.connected.is_set())
        assert dropped == [("H0UNCNT0", "005930")] and session.data_subscriptions() == []
        with pytest.raises(SubscriptionLimit):
            await session.add("H0UNCNT0", "005930")  # 연결 중 KIS 거부(OPSP0008)도 SubscriptionLimit
        assert "hts" not in str(session.health())  # HTS ID 는 상태 조회에 나오지 않음
        await session.stop()


# ───────────── 허브 ─────────────
class FakeSource:
    def __init__(self, limit: int = 3) -> None:
        self.limit = limit
        self.handlers: dict = {}
        self.subs: list[tuple[str, str]] = []
        self.on_dropped = None

    def set_handler(self, tr_id: str, handler) -> None:
        self.handlers[tr_id] = handler

    async def add(self, tr_id: str, tr_key: str, kind: str = "data") -> None:
        if len(self.subs) >= self.limit:
            raise SubscriptionLimit("한도")
        self.subs.append((tr_id, tr_key))

    async def remove(self, tr_id: str, tr_key: str) -> None:
        self.subs.remove((tr_id, tr_key))

    async def push(self, tr_id: str, records: list[list[str]]) -> None:
        await self.handlers[tr_id](RealtimeMessage(tr_id, False, records))


def _hub(home: Path, config: MarketDataConfig, sources: dict) -> MarketDataHub:
    settings = load_settings(home)
    db = Database.memory()
    db.create_all()
    reg = InstrumentRegistry([Instrument("US:AAPL", "US", "AAPL", "NASD", "Apple")])
    profiles = {a: acc.profile for a, acc in settings.accounts.items()}
    return MarketDataHub(sources, profiles, InstrumentCatalog(db, reg), config, clock=Clock(KR_OPEN))


async def test_hub_refcount_source_selection_and_limit(home: Path) -> None:
    real, paper = FakeSource(limit=1), FakeSource(limit=40)
    hub = _hub(home, MarketDataConfig(kr_account="paper", us_account="real"), {"real": real, "paper": paper})
    got: list = []
    hub.listeners.append(lambda ch, item: got.append((ch, item)))

    assert await hub.acquire("tick:kr:005930") == "tick:KR:005930"
    assert await hub.acquire("tick:KR:005930") == "tick:KR:005930"
    assert paper.subs == [("H0STCNT0", "005930")]  # 모의 계좌는 KRX 시세, 등록은 한 번

    assert await hub.acquire("tick:US:AAPL") == "tick:US:AAPL"
    assert real.subs == [("HDFSCNT0", "DNASAAPL")]
    with pytest.raises(OmsError) as e:
        await hub.acquire("orderbook:US:AAPL")
    assert e.value.code == "SUBSCRIPTION_LIMIT"
    with pytest.raises(OmsError):
        await hub.acquire("tick:US:MSFT")  # 미등록 미국 종목

    await paper.push("H0STCNT0", [kr_tick(price="61000"), kr_tick(price="61100")])
    assert [ch for ch, _ in got] == ["tick:KR:005930", "tick:KR:005930"]
    assert hub.last_price("KR:005930") == D("61100")

    await hub.release("tick:KR:005930")
    assert paper.subs == [("H0STCNT0", "005930")]
    await hub.release("tick:KR:005930")
    assert paper.subs == []


async def test_hub_unified_tr_when_account_supports_sor(home: Path) -> None:
    real = FakeSource()
    hub = _hub(home, MarketDataConfig(), {"real": real})
    await hub.acquire("tick:KR:005930")
    await hub.acquire("orderbook:KR:005930")
    assert real.subs == [("H0UNCNT0", "005930"), ("H0UNASP0", "005930")]


# ───────────── 틱 저장 ─────────────
def test_tick_store_flush_read_compact(tmp_path: Path) -> None:
    store = TickStore(tmp_path, clock=Clock(KR_OPEN))
    for i, price in enumerate(["61000", "61100", "61050"]):
        store.on_market("tick:KR:005930", rt.parse_kr_tick("H0STCNT0", kr_tick(price=price, hhmmss=f"10000{i}"), TODAY))
    assert store.flush() == 3
    store.on_market("tick:KR:005930", rt.parse_kr_tick("H0STCNT0", kr_tick(price="61200", hhmmss="100009"), TODAY))
    rows = store.read("KR:005930", TODAY)  # 파일 + 아직 쓰지 않은 버퍼
    assert [r["price"] for r in rows] == [D("61000"), D("61100"), D("61050"), D("61200")]
    store.flush()
    assert store.compact(TODAY) == 1
    files = [p.name for p in (tmp_path / "KR" / "20261014" / "005930").iterdir()]
    assert files == ["ticks.parquet"]
    assert len(store.read("KR:005930", TODAY)) == 4 and store.read("KR:005930", TODAY, limit=1)[0]["price"] == D("61200")


# ───────────── API ─────────────
def test_api_ws_market_channel_and_quote(home: Path) -> None:
    clock = Clock(KR_OPEN)
    sim = SimBroker("real", clock=clock)
    sim.set_quote("KR:005930", D("60000"), upper=D("78000"), lower=D("42000"))
    real = FakeSource()
    app = CentralApp(load_settings(home), db=Database.memory(), brokers={"real": sim, "paper": SimBroker("paper")},
                     clock=clock, realtime={"real": real})
    api = create_app(app, manage_lifecycle=False)
    with TestClient(api, base_url="http://test") as tc:
        sid = tc.post("/api/v1/admin/sub-accounts", headers=ADMIN,
                      json={"broker_account_id": "real", "name": "A", "markets": ["KR"]}).json()["data"]["id"]
        key = tc.post(f"/api/v1/admin/sub-accounts/{sid}/api-keys", headers=ADMIN).json()["data"]["api_key"]
        h = {"X-API-Key": key}
        assert tc.get("/api/v1/market/quote/KR:005930", headers=h).json()["data"]["source"] == "rest"

        with tc.websocket_connect("/api/v1/ws", headers=h) as ws:
            ws.receive_json()
            ws.send_json({"op": "subscribe", "channels": ["tick:KR:005930", "tick:KR:005930"]})
            assert ws.receive_json()["data"]["channels"][-1] == "tick:KR:005930"
            assert real.subs == [("H0UNCNT0", "005930")]
            tc.portal.call(real.push, "H0UNCNT0", [kr_tick(price="61500", hhmmss="100000")])
            msg = ws.receive_json()
            assert msg["ch"] == "tick:KR:005930" and msg["data"]["price"] == "61500" and msg["data"]["side"] == "buy"

            q = tc.get("/api/v1/market/quote/KR:005930", headers=h).json()["data"]
            assert q["source"] == "realtime" and q["last"] == "61500"
            rows = tc.get("/api/v1/market/ticks/KR:005930", headers=h).json()["data"]["rows"]
            assert [r["price"] for r in rows] == ["61500.0000"]
            stats = tc.get("/api/v1/admin/market-data", headers=ADMIN).json()["data"]
            assert stats["channels"] == {"tick:KR:005930": 1} and stats["sources"]["KR"] == "real"

            ws.send_json({"op": "subscribe", "channels": ["orderbook:KR:005930", "bad"]})
            codes = [ws.receive_json()["data"].get("code") for _ in range(2)]
            assert codes == ["INVALID_REQUEST", None]
        # 연결이 끊기면 그 연결이 잡고 있던 시세 채널을 해제한다
        assert real.subs == []
