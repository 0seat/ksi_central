"""M5-B: 캔들 캐시/집계, 실시간 1분봉 채널, 캔들/호가 API."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest

from central.api.server import create_app
from central.app import CentralApp
from central.broker.base import Candle
from central.broker.sim import SimBroker
from central.core.instruments import Instrument
from central.core.market_hours import KST
from central.marketdata import candles as cmod
from central.marketdata.candles import CandleService, aggregate
from central.marketdata.hub import LiveBar
from central.settings import MarketDataConfig, load_settings
from central.storage.db import Database
from tests.oms_env import Clock
from tests.test_marketdata import FakeSource, _hub, kr_tick

ADMIN = {"X-Admin-Key": "dummy-admin-key-0000000000"}
SAMSUNG = Instrument("KR:005930", "KR", "005930", "KRX", "삼성전자")
AAPL = Instrument("US:AAPL", "US", "AAPL", "NASD", "Apple")


def bar(ts: datetime, price: int, vol: int = 10) -> Candle:
    p = D(price)
    return Candle(ts, p, p + 100, p - 100, p, vol)


def kr_minutes(day: datetime, start: str = "0900", n: int = 390) -> list[Candle]:
    t0 = datetime.strptime(day.strftime("%Y%m%d") + start, "%Y%m%d%H%M").replace(tzinfo=KST)
    return [bar(t0 + timedelta(minutes=i), 60000 + i) for i in range(n)]


# ───────────── 집계 ─────────────
def test_aggregate_kr_5m_aligned_to_open() -> None:
    bars = kr_minutes(datetime(2026, 10, 14, tzinfo=KST), n=12)   # 09:00 ~ 09:11
    out = aggregate(bars, 5, "KR")
    assert [b.ts.strftime("%H%M") for b in out] == ["0900", "0905", "0910"]
    assert (out[0].open, out[0].close, out[0].volume) == (D(60000), D(60004), 50)
    assert out[0].high == D(60104) and out[0].low == D(59900)


def test_aggregate_us_60m_aligned_to_0930_et() -> None:
    # 2026-10-14 09:30 EDT = 22:30 KST
    t0 = datetime(2026, 10, 14, 22, 30, tzinfo=KST)
    bars = [bar(t0 + timedelta(minutes=i), 100 + i) for i in range(90)]
    out = aggregate(bars, 60, "US")
    assert [b.ts.strftime("%H%M") for b in out] == ["2230", "2330"] and out[0].volume == 600


# ───────────── 캐시 ─────────────
def _svc(now: datetime) -> tuple[CandleService, SimBroker, Database]:
    db = Database.memory()
    db.create_all()
    sim = SimBroker("real", clock=Clock(now))
    return CandleService(db, lambda market: sim, Clock(now)), sim, db


async def test_minute_candles_cached_and_coverage_reused() -> None:
    now = datetime(2026, 10, 14, 10, 0, 30, tzinfo=KST)
    svc, sim, _ = _svc(now)
    sim.candles[(SAMSUNG.id, "1m")] = kr_minutes(datetime(2026, 10, 13, tzinfo=KST)) + kr_minutes(
        datetime(2026, 10, 14, tzinfo=KST), n=61)   # 오늘 09:00 ~ 10:00 (10:00 봉은 진행 중)

    start = datetime(2026, 10, 13, 14, 0, tzinfo=KST)
    r1 = await svc.get(SAMSUNG, "1m", start, now)
    assert r1.truncated is False and r1.kis_calls >= 2
    assert r1.candles[0].ts == start and r1.candles[-1].ts == datetime(2026, 10, 14, 10, 0, tzinfo=KST)

    # 과거 구간은 캐시에서만
    r2 = await svc.get(SAMSUNG, "1m", start, datetime(2026, 10, 14, 9, 30, tzinfo=KST))
    assert r2.kis_calls == 0 and len(r2.candles) == 90 + 31   # 13일 14:00~15:29, 14일 09:00~09:30

    # 최신 구간을 다시 요청하면 캐시 끝부터 한 페이지만 받는다
    calls = sim.candle_calls
    r3 = await svc.get(SAMSUNG, "5m", start, now)
    assert sim.candle_calls - calls == 1 and r3.candles[-1].ts == datetime(2026, 10, 14, 10, 0, tzinfo=KST)

    from central.storage.models import CandleCoverageRow
    with svc.db.session() as s:
        assert s.query(CandleCoverageRow).count() == 1   # 같은 시작의 구간 기록은 하나로 유지


async def test_unclosed_bars_not_cached() -> None:
    now = datetime(2026, 10, 14, 9, 5, 30, tzinfo=KST)
    svc, sim, db = _svc(now)
    sim.candles[(SAMSUNG.id, "1m")] = kr_minutes(datetime(2026, 10, 14, tzinfo=KST), n=6)  # 09:00~09:05
    await svc.get(SAMSUNG, "1m", datetime(2026, 10, 14, 9, 0, tzinfo=KST), now)
    from central.storage.models import CandleRow
    with db.session() as s:
        cached = sorted(r.ts.strftime("%H%M") for r in s.query(CandleRow))
    assert cached == ["0900", "0901", "0902", "0903"]   # 09:04 는 방금 닫혔을 수도 있어 1분 여유, 09:05 는 진행 중


async def test_page_limit_marks_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 10, 14, 15, 40, tzinfo=KST)
    svc, sim, _ = _svc(now)
    sim.candle_page_size = 10
    sim.candles[(SAMSUNG.id, "1m")] = kr_minutes(datetime(2026, 10, 14, tzinfo=KST))
    monkeypatch.setitem(cmod.MAX_PAGES, "1m", 3)
    r = await svc.get(SAMSUNG, "1m", datetime(2026, 10, 14, 9, 0, tzinfo=KST), now)
    assert r.truncated is True and r.kis_calls == 3 and len(r.candles) == 30
    # 잘린 요청은 coverage 를 남기지 않으므로 다음 요청에서 다시 받는다
    r2 = await svc.get(SAMSUNG, "1m", datetime(2026, 10, 14, 9, 0, tzinfo=KST), datetime(2026, 10, 14, 15, 0, tzinfo=KST))
    assert r2.kis_calls > 0


async def test_daily_today_not_cached_until_next_day() -> None:
    now = datetime(2026, 10, 14, 11, 0, tzinfo=KST)
    svc, sim, _ = _svc(now)
    days = [datetime(2026, 10, d, tzinfo=KST) for d in (8, 9, 10, 13, 14)]
    sim.candles[(SAMSUNG.id, "1d")] = [bar(d, 60000) for d in days]
    r = await svc.get(SAMSUNG, "1d", datetime(2026, 10, 1, tzinfo=KST), now)
    assert [c.ts.day for c in r.candles] == [8, 9, 10, 13, 14]
    r2 = await svc.get(SAMSUNG, "1d", datetime(2026, 10, 1, tzinfo=KST), datetime(2026, 10, 13, 23, 0, tzinfo=KST))
    assert r2.kis_calls == 0 and [c.ts.day for c in r2.candles] == [8, 9, 10, 13]


async def test_us_minutes_page_through_after_hours() -> None:
    """최신 페이지가 전부 시간외 봉이어도 정규장 봉까지 계속 받고, 결과에는 정규장 봉만 (2026-10-14 실측 버그)."""
    now = datetime(2026, 10, 14, 11, 0, tzinfo=KST)                  # 10-13 22:00 EDT, 시간외 끝난 뒤
    svc, sim, _ = _svc(now)
    sim.candle_page_size = 120
    t0 = datetime(2026, 10, 13, 22, 30, tzinfo=KST)                  # 10-13 09:30 EDT
    regular = [bar(t0 + timedelta(minutes=i), 340) for i in range(390)]
    after = [bar(t0 + timedelta(minutes=390 + i), 341) for i in range(240)]   # 16:00~20:00 EDT
    sim.candles[(AAPL.id, "1m")] = regular + after
    r = await svc.get(AAPL, "1m", datetime(2026, 10, 13, 22, 30, tzinfo=KST), now)
    assert len(r.candles) == 390 and r.truncated is False
    assert r.candles[-1].ts == datetime(2026, 10, 14, 4, 59, tzinfo=KST)   # 15:59 EDT


# ───────────── 실시간 1분봉 ─────────────
async def test_live_candle_channel_shares_tick_feed(home: Path) -> None:
    src = FakeSource()
    hub = _hub(home, MarketDataConfig(), {"real": src})
    got: list = []
    hub.listeners.append(lambda ch, item: got.append((ch, item)))
    await hub.acquire("candle.1m:KR:005930")
    await hub.acquire("tick:KR:005930")
    assert src.subs == [("H0UNCNT0", "005930")]   # 1분봉과 체결이 등록 하나를 공유

    await src.push("H0UNCNT0", [kr_tick(price="61000", qty="5", hhmmss="100001"),
                                kr_tick(price="61200", qty="3", hhmmss="100040"),
                                kr_tick(price="60900", qty="2", hhmmss="100105")])
    bars = [item for ch, item in got if ch == "candle.1m:KR:005930"]
    assert isinstance(bars[-1], LiveBar)
    closed = [b for b in bars if b.closed]
    assert len(closed) == 1 and (closed[0].open, closed[0].high, closed[0].low, closed[0].close, closed[0].volume) == (
        D(61000), D(61200), D(61000), D(61200), 8)
    assert bars[-1].ts.strftime("%H%M") == "1001" and bars[-1].closed is False

    await hub.release("candle.1m:KR:005930")
    assert src.subs == [("H0UNCNT0", "005930")]   # 체결 채널이 남아 있어 유지
    await hub.release("tick:KR:005930")
    assert src.subs == []


# ───────────── API ─────────────
async def test_candles_and_orderbook_api(home: Path) -> None:
    now = datetime(2026, 10, 14, 10, 0, 30, tzinfo=KST)
    clock = Clock(now)
    sim = SimBroker("real", clock=clock)
    sim.set_quote(SAMSUNG.id, D("61000"))
    sim.candles[(SAMSUNG.id, "1m")] = kr_minutes(datetime(2026, 10, 14, tzinfo=KST), n=61)
    app = CentralApp(load_settings(home), db=Database.memory(), brokers={"real": sim, "paper": SimBroker("paper")},
                     clock=clock, realtime={})
    api = create_app(app, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test/api/v1") as http:
        res = (await http.get("/market/candles/KR:005930", headers=ADMIN,
                              params={"interval": "15m", "from": "2026-10-14"})).json()["data"]
        assert [c["ts"][11:16] for c in res["candles"]] == ["09:00", "09:15", "09:30", "09:45", "10:00"]
        assert res["candles"][0]["volume"] == 150 and res["truncated"] is False
        res = (await http.get("/market/candles/KR:005930", headers=ADMIN,
                              params={"interval": "1m", "from": "2026-10-14", "limit": 2})).json()["data"]
        assert [c["ts"][11:16] for c in res["candles"]] == ["09:59", "10:00"]
        bad = await http.get("/market/candles/KR:005930", headers=ADMIN, params={"interval": "7m"})
        assert bad.status_code == 400
        ob = (await http.get("/market/orderbook/KR:005930", headers=ADMIN)).json()["data"]
        assert ob["source"] == "rest" and ob["asks"][0] == ["61000", 10]
    await app.http.aclose()
