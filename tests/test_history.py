"""과거 시세 보관소: 이벤트 감지(분할/병합/배당), 수정 적용, 저장, 가져오기."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from central.broker.base import Candle
from central.core.instruments import Instrument
from central.core.market_hours import KST, NEW_YORK
from central.marketdata.history import (
    Event,
    HistoryFetcher,
    HistoryStore,
    SeriesKey,
    adjust,
    detect_events,
    reconstruction_error,
    regular_only,
)
from tests.oms_env import Clock

AAPL = Instrument("US:AAPL", "US", "AAPL", "NASD", "Apple")
SAMSUNG = Instrument("KR:005930", "KR", "005930", "KRX", "삼성전자")


def day_bar(d: date, close: str, vol: int = 1000) -> Candle:
    c = D(close)
    return Candle(datetime(d.year, d.month, d.day, tzinfo=KST), c, c, c, c, vol)


def series(start: date, closes: list[str]) -> list[Candle]:
    out, d = [], start
    for c in closes:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append(day_bar(d, c))
        d += timedelta(days=1)
    return out


def scaled(bars: list[Candle], factors: list[D], quant: str) -> list[Candle]:
    """수정주가 흉내: 원주가 x 계수를 KIS 단위로 반올림."""
    q = D(quant)
    return [Candle(b.ts, *(((x * f).quantize(q)) for x in (b.open, b.high, b.low, b.close)), b.volume)
            for b, f in zip(bars, factors)]


# ───────────── 키 ─────────────
def test_series_key_parse() -> None:
    assert str(SeriesKey.parse("kr:005930")) == "KR:005930"
    k = SeriesKey.parse("INDEX:US:.DJI")
    assert k.index and k.market == "US" and k.folder.as_posix() == "INDEX_US/_DJI"
    with pytest.raises(ValueError):
        SeriesKey.parse("JP:7203")


# ───────────── 이벤트 감지 ─────────────
def test_us_split_and_dividends_detected() -> None:
    """미국: 4:1 분할은 정확히 1/4 로, 배당은 소수 비율로. 원주가 x 이벤트 = KIS 수정주가."""
    closes = [str(400 + i) for i in range(30)] + [str(105 + i) for i in range(30)] + [str(140 + i) for i in range(30)]
    raw = series(date(2020, 7, 1), closes)
    # 30번째 날 분할(1/4), 60번째 날 배당(0.995). 수정 계수는 최근이 1
    f = [D("0.25") * D("0.995")] * 30 + [D("0.995")] * 30 + [D(1)] * 30
    adj = scaled(raw, f, "0.0001")
    events = detect_events(raw, adj, "US")
    assert [(e.date, e.kind) for e in events] == [(raw[30].ts.date(), "split"), (raw[60].ts.date(), "dividend")]
    assert events[0].ratio == D("0.25")
    assert abs(events[1].ratio - D("0.995")) < D("0.00001")
    assert reconstruction_error(raw, adj, events, "US")["days_off"] == 0


def test_kr_reverse_split_and_blip_ignored() -> None:
    """국내 1:10 병합(k=10), 하루짜리 비율 튐과 거래량 0(거래정지)인 날은 무시."""
    raw = series(date(2024, 3, 4), ["1000"] * 20 + ["10000"] * 20)
    f = [D(10)] * 20 + [D(1)] * 20
    adj = scaled(raw, f, "1")
    adj[30] = Candle(adj[30].ts, *([adj[30].close * D("1.05")] * 4), adj[30].volume)   # KIS 자료 튐
    raw[19] = Candle(raw[19].ts, raw[19].open, raw[19].high, raw[19].low, raw[19].close, 0)   # 거래정지
    events = detect_events(raw, adj, "KR")
    assert events == [Event(raw[20].ts.date(), D(10), "split")]


def test_no_events_when_ratio_constant() -> None:
    raw = series(date(2025, 1, 2), [str(100 + i % 7) for i in range(50)])
    assert detect_events(raw, scaled(raw, [D(1)] * 50, "0.0001"), "US") == []


# ───────────── 수정 적용 ─────────────
def test_adjust_modes_and_volume() -> None:
    raw = [day_bar(date(2020, 8, 28), "499.23", 1000), day_bar(date(2020, 8, 31), "129.04", 4000)]
    events = [Event(date(2020, 8, 31), D("0.25"), "split"), Event(date(2020, 8, 31), D("0.99"), "dividend")]
    split = adjust(raw, events, "split", KST)
    total = adjust(raw, events, "total", KST)
    assert split[0].close == D("124.8075") and split[0].volume == 4000      # 거래량은 분할 비율로만
    assert total[0].close == D("123.5594") and total[0].volume == 4000
    assert split[1] == raw[1] and adjust(raw, events, "none", KST) == raw


def test_minute_bars_use_local_trade_date() -> None:
    """미국 분봉은 뉴욕 거래일로 이벤트와 비교한다 (KST 로는 다음 날 새벽인 봉)."""
    ev = [Event(date(2020, 8, 31), D("0.25"), "split")]
    before = Candle(datetime(2020, 8, 28, 15, 59, tzinfo=NEW_YORK).astimezone(KST), D(500), D(500), D(500), D(500), 4)
    on = Candle(datetime(2020, 8, 31, 9, 30, tzinfo=NEW_YORK).astimezone(KST), D(128), D(128), D(128), D(128), 4)
    out = adjust([before, on], ev, "split", NEW_YORK)
    assert out[0].close == D(125) and out[0].volume == 16 and out[1].close == D(128)


# ───────────── 저장소 ─────────────
def kr_minutes(day: date, start: str, n: int, price: int = 100) -> list[Candle]:
    t0 = datetime.strptime(day.strftime("%Y%m%d") + start, "%Y%m%d%H%M").replace(tzinfo=KST)
    return [Candle(t0 + timedelta(minutes=i), D(price), D(price), D(price), D(price), 1) for i in range(n)]


def test_store_minutes_merge_and_regular_filter(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    key = SeriesKey("KR", "005930")
    normal = kr_minutes(date(2025, 10, 31), "0900", 400)              # 09:00 ~ 15:39 (15:31~ 은 시간외 취급)
    suneung = kr_minutes(date(2025, 11, 13), "1000", 391)             # 수능일 10:00 ~ 16:30
    assert store.save_minutes(key, normal + suneung) == 791
    assert store.save_minutes(key, normal[:10]) == 0                   # 같은 시각은 덮어쓰기
    assert sorted(p.stem for p in (tmp_path / "KR" / "005930" / "1m").glob("*.parquet")) == ["2025-10", "2025-11"]
    bars = store.minutes(key)
    oct_ = [b for b in bars if b.ts.month == 10]
    nov = [b for b in bars if b.ts.month == 11]
    assert oct_[-1].ts.strftime("%H:%M") == "15:30" and len(oct_) == 391
    assert nov[0].ts.strftime("%H:%M") == "10:00" and nov[-1].ts.strftime("%H:%M") == "16:30"
    assert len(store.minutes(key, regular=False)) == 791
    assert store.last_minute(key) == suneung[-1].ts


def test_store_read_applies_events(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    key = SeriesKey("US", "AAPL")
    store.save_daily(key, [day_bar(date(2020, 8, 28), "499.23"), day_bar(date(2020, 8, 31), "129.04")])
    store.save_events(key, [Event(date(2020, 8, 31), D("0.25"), "split")], datetime.now(KST))
    assert [b.close for b in store.read(key, "1d", None, None, "split")] == [D("124.8075"), D("129.04")]
    assert [b.close for b in store.read(key, "1d", None, None, "none")] == [D("499.23"), D("129.04")]
    assert store.symbols() == ["US:AAPL"]


def test_regular_only_us() -> None:
    key = SeriesKey("US", "AAPL")
    pre = Candle(datetime(2026, 10, 13, 9, 29, tzinfo=NEW_YORK), D(1), D(1), D(1), D(1), 1)
    reg = Candle(datetime(2026, 10, 13, 9, 30, tzinfo=NEW_YORK), D(1), D(1), D(1), D(1), 1)
    assert regular_only(key, [pre, reg]) == [reg]


# ───────────── API ─────────────
async def test_history_api(home: Path) -> None:
    import httpx

    from central.api.server import create_app
    from central.app import CentralApp
    from central.broker.sim import SimBroker
    from central.marketdata.history_cli import history_root
    from central.settings import load_settings
    from central.storage.db import Database

    settings = load_settings(home)
    store = HistoryStore(history_root(settings))
    key = SeriesKey("US", "AAPL")
    store.save_daily(key, [day_bar(date(2020, 8, 28), "499.23"), day_bar(date(2020, 8, 31), "129.04")])
    store.save_events(key, [Event(date(2020, 8, 31), D("0.25"), "split")], datetime.now(KST))
    app = CentralApp(settings, db=Database.memory(), brokers={"real": SimBroker("real"), "paper": SimBroker("paper")},
                     realtime={})
    api = create_app(app, manage_lifecycle=False)
    headers = {"X-Admin-Key": "dummy-admin-key-0000000000"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test/api/v1") as http:
        res = (await http.get("/market/history/US:AAPL", headers=headers,
                              params={"adjust": "split", "from": "2020-08-01"})).json()["data"]
        assert [c["close"] for c in res["candles"]] == ["124.8075", "129.04"]
        assert res["events"] == [{"date": "2020-08-31", "ratio": "0.25", "kind": "split"}]
        bad = await http.get("/market/history/JP:7203", headers=headers)
        assert bad.status_code == 400
    await app.http.aclose()


# ───────────── 일정 작업 ─────────────
async def test_ops_history_job(home: Path, tmp_path: Path) -> None:
    from central.marketdata import history as hmod
    from tests.test_ops import make_app

    clock = Clock(datetime(2026, 10, 14, 16, 15, tzinfo=KST))
    app, _, _, rec = make_app(home, clock)
    app.settings = app.settings.model_copy(update={"history": app.settings.history.model_copy(
        update={"symbols": ["KR:005930"], "indices": ["INDEX:KR:0001"]})})
    app.ops.scheduler.jobs.clear()
    app.ops._register_jobs()
    assert "history_kr" in app.ops.scheduler.jobs and "history_us" not in app.ops.scheduler.jobs

    raw = series(date(2026, 9, 1), ["100"] * 30)
    fake = FakeKis(raw, [D(1)] * 30, kr_minutes(date(2026, 10, 14), "0900", 381))

    async def index_daily(market, code, end):
        return [b for b in raw if b.ts.date() <= end]

    async def index_minutes(market, code):
        return kr_minutes(date(2026, 10, 14), "1400", 60)

    fake.index_daily, fake.index_minutes = index_daily, index_minutes   # type: ignore[method-assign]
    store = hmod.HistoryStore(tmp_path)
    app.ops._history_fetcher = lambda: hmod.HistoryFetcher(store, lambda m: fake, clock)   # type: ignore[method-assign]
    result = await app.ops.scheduler.run("history_kr")
    assert result["ok"], result
    assert len(store.minutes(SeriesKey("KR", "005930"))) == 381
    assert len(store.daily(SeriesKey("KR", "0001", True))) == 30
    await app.http.aclose()


# ───────────── 가져오기 ─────────────
class FakeKis:
    """history_* 를 흉내: 원주가/수정주가 일봉과 1분봉을 페이지로 준다."""

    def __init__(self, raw: list[Candle], factors: list[D], minutes: list[Candle] | None = None) -> None:
        self.raw, self.factors, self.minute_bars = raw, factors, minutes or []
        self.calls = 0

    async def history_daily(self, inst, end: date, adjusted: bool):
        self.calls += 1
        bars = scaled(self.raw, self.factors, "0.0001") if adjusted else self.raw
        return [b for b in bars if b.ts.date() <= end][-100:]

    async def history_minutes(self, inst, end: datetime):
        self.calls += 1
        return [b for b in self.minute_bars if b.ts <= end][-120:]


async def test_fetcher_full_then_incremental_detects_new_split(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    raw = series(date(2024, 1, 2), [str(100 + i % 5) for i in range(260)])
    now = datetime.combine(raw[-1].ts.date() + timedelta(days=3), datetime.min.time(), KST).replace(hour=12)
    fake = FakeKis(raw, [D(1)] * 260)
    fetcher = HistoryFetcher(store, lambda m: fake, Clock(now))
    r1 = await fetcher.daily(AAPL)
    assert r1["added"] == 260 and r1["events"] == 0

    # 이후 2:1 분할: 새 원주가는 절반, KIS 수정주가는 과거 전체가 0.5 배로 바뀐다
    more = series(raw[-1].ts.date() + timedelta(days=1), [str(50 + i % 3) for i in range(20)])
    fake.raw = raw + more
    fake.factors = [D("0.5")] * 260 + [D(1)] * 20
    later = now + timedelta(days=30)
    fetcher.clock = Clock(later)
    fake.calls = 0
    r2 = await fetcher.daily(AAPL)
    assert fake.calls <= 4                                              # 최근 구간만
    assert store.events(SeriesKey("US", "AAPL")) == [Event(more[0].ts.date(), D("0.5"), "split")]
    assert r2["check"]["days_off"] == 0
    split = store.read(SeriesKey("US", "AAPL"), "1d", None, None, "split")
    assert split[0].close == raw[0].close / 2


async def test_fetcher_skips_today_daily_bar(tmp_path: Path) -> None:
    raw = series(date(2026, 10, 5), ["10"] * 8)
    today = raw[-1].ts.date()
    fake = FakeKis(raw, [D(1)] * 8)
    fetcher = HistoryFetcher(HistoryStore(tmp_path), lambda m: fake, Clock(datetime.combine(today, datetime.min.time(), KST).replace(hour=11)))
    await fetcher.daily(SAMSUNG)
    assert fetcher.store.daily(SeriesKey("KR", "005930"))[-1].ts.date() < today


async def test_fetcher_minutes_stop_at_stored(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    bars = kr_minutes(date(2026, 10, 13), "0900", 381) + kr_minutes(date(2026, 10, 14), "0900", 381)
    fake = FakeKis([], [], bars[:381])
    now = datetime(2026, 10, 13, 20, 0, tzinfo=KST)
    fetcher = HistoryFetcher(store, lambda m: fake, Clock(now))
    r1 = await fetcher.minutes(SAMSUNG)
    assert r1["added"] == 381
    fake.minute_bars, fake.calls = bars, 0
    fetcher.clock = Clock(datetime(2026, 10, 14, 20, 0, tzinfo=KST))
    r2 = await fetcher.minutes(SAMSUNG)
    assert r2["added"] == 381 and fake.calls <= 5
