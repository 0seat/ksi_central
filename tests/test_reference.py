"""M5-C: 영업일 캘린더, 종목 마스터."""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal as D
from pathlib import Path

import httpx

from central.api.server import create_app
from central.app import CentralApp
from central.broker.sim import SimBroker
from central.core.instruments import Instrument, InstrumentRegistry, is_tick_aligned
from central.core.market_hours import KST, MarketHours
from central.marketdata.calendar import CalendarService, us_early_closes, us_holidays
from central.marketdata.master import MasterItem, parse_kr, parse_us
from central.oms.catalog import InstrumentCatalog
from central.settings import load_settings
from central.storage.db import Database
from tests.oms_env import Clock

ADMIN = {"X-Admin-Key": "dummy-admin-key-0000000000"}


# ───────────── 미국 규칙 ─────────────
def test_us_holidays_2026() -> None:
    assert sorted(us_holidays(2026)) == [
        date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25),
        date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25)]
    # 7/4 가 토요일이라 7/3 이 휴장 → 독립기념일 전날 조기폐장 없음
    assert us_early_closes(2026) == {date(2026, 11, 27): time(13, 0), date(2026, 12, 24): time(13, 0)}


def test_us_holiday_weekend_rules() -> None:
    h2022 = us_holidays(2022)
    assert date(2021, 12, 31) not in us_holidays(2021) and date(2022, 1, 1) not in h2022  # 토요일 신정: 대체 없음
    assert date(2022, 12, 26) in h2022            # 일요일 성탄절 → 월요일
    assert date(2025, 4, 18) in us_holidays(2025)  # 성금요일 (부활절 4/20)
    assert date(2025, 7, 3) in us_early_closes(2025)


def test_us_early_close_blocks_orders() -> None:
    hours = MarketHours(us_early_close={date(2026, 11, 27): time(13, 0)})
    # 2026-11-27 12:30 EST = 11-28 02:30 KST, 13:30 EST = 03:30 KST
    assert hours.can_place_order("US", datetime(2026, 11, 28, 2, 30, tzinfo=KST))[0] is True
    ok, reason = hours.can_place_order("US", datetime(2026, 11, 28, 3, 30, tzinfo=KST))
    assert ok is False and "13:00" in reason


# ───────────── 국내 휴장일 ─────────────
async def test_kr_calendar_refresh_once_a_day_and_apply() -> None:
    db = Database.memory()
    db.create_all()
    clock = Clock(datetime(2026, 10, 1, 8, 0, tzinfo=KST))
    sim = SimBroker("real", clock=clock)
    sim.kr_days = {date(2026, 10, d): d not in (3, 4, 5, 9, 10, 11) for d in range(1, 13)}
    hours = MarketHours()
    cal = CalendarService(db, hours, lambda: sim, clock)
    assert await cal.refresh_kr() == 12
    assert await cal.refresh_kr() == 0 and sim.market_day_calls == 1   # 같은 날 두 번째는 호출 안 함
    assert {date(2026, 10, 5), date(2026, 10, 9)} <= hours.holidays["KR"]
    ok, reason = hours.can_place_order("KR", datetime(2026, 10, 9, 10, 0, tzinfo=KST))
    assert ok is False and "휴장" in reason

    days = {d["date"]: d for d in cal.days("KR", date(2026, 10, 8), date(2026, 10, 14))}
    assert days[date(2026, 10, 8)]["open"] is True and days[date(2026, 10, 8)]["source"] == "kis"
    assert days[date(2026, 10, 9)]["open"] is False
    assert days[date(2026, 10, 13)]["source"] == "weekday"   # 아직 받지 않은 날
    us = {d["date"]: d for d in cal.days("US", date(2026, 11, 26), date(2026, 11, 27))}
    assert us[date(2026, 11, 26)]["open"] is False and us[date(2026, 11, 27)]["early_close_et"] == "13:00"

    # 다음 날에는 다시 받는다 (새 DB 인스턴스로 재시작해도 DB 기록으로 판단)
    clock.t = datetime(2026, 10, 2, 8, 0, tzinfo=KST)
    await CalendarService(db, MarketHours(), lambda: sim, clock).refresh_kr()
    assert sim.market_day_calls == 2


# ───────────── 종목 마스터 ─────────────
def test_parse_kr_master_line() -> None:
    tail = "ST" + "0" * 225          # 코스피 꼬리 227자 (개행 제외)
    line = "005930   KR7005930003삼성전자" + " " * 20 + tail
    etf = "069500   KR7069500007KODEX 200" + " " * 10 + "EF" + "0" * 225
    items = parse_kr("\n".join([line, etf]), 227)
    assert [(i.code, i.name, i.kind) for i in items] == [("005930", "삼성전자", "stock"), ("069500", "KODEX 200", "etf")]


def test_parse_us_master_line() -> None:
    def row(sym: str, kor: str, eng: str, kind: str) -> str:
        f = ["US", "22", "NAS", "나스닥", sym, "NAS" + sym, kor, eng, kind] + ["USD", "4"] + [""] * 13
        return "\t".join(f)
    text = "\n".join([row("AAPL", "애플", "APPLE INC", "2"), row("QQQ", "QQQ", "INVESCO QQQ", "3"),
                      row("NDX", "나스닥100", "NASDAQ 100", "1")])
    items = parse_us(text, "NASD")
    assert [(i.code, i.exchange, i.kind, i.name_en) for i in items] == [
        ("AAPL", "NASD", "stock", "APPLE INC"), ("QQQ", "NASD", "etf", "INVESCO QQQ")]   # 지수 제외


def test_bulk_load_keeps_manual_nxt_flag_and_resolves() -> None:
    db = Database.memory()
    db.create_all()
    cat = InstrumentCatalog(db, InstrumentRegistry())
    cat.upsert("KR", "005930", name="삼성전자", nxt_tradable=True)
    n = cat.bulk_load([MasterItem("KR", "005930", "KRX", "삼성전자", "", "stock"),
                       MasterItem("KR", "069500", "KRX", "KODEX 200", "", "etf"),
                       MasterItem("US", "BRK/B", "NYSE", "버크셔 해서웨이 B", "BERKSHIRE HATHAWAY INC", "stock"),
                       MasterItem("US", "AAPL", "NASD", "애플", "APPLE INC", "stock")])
    assert n == 4 and cat.has_master() and cat.counts() == {"KR": 2, "US": 2}
    assert cat.resolve("KR:005930").nxt_tradable is True        # 수동 지정 유지
    assert cat.resolve("us:brk/b").exchange == "NYSE"
    assert [x["id"] for x in cat.search("apple")] == ["US:AAPL"]
    assert cat.search("005930")[0]["id"] == "KR:005930"
    cat.bulk_load([MasterItem("KR", "0219E0", "KRX", "KODEX 200커버드콜", "", "etf")])
    assert cat.search("KODEX 200")[0]["id"] == "KR:069500"   # 이름이 정확히 같은 종목 먼저
    etf = cat.resolve("KR:069500")
    assert etf.kind == "etf" and is_tick_aligned(etf, D("35015"))   # ETF 는 원 단위만 검사 (KIS 판단에 맡김)
    assert not is_tick_aligned(cat.resolve("KR:005930"), D("35015"))


# ───────────── API ─────────────
async def test_calendar_and_slash_symbol_api(home: Path) -> None:
    clock = Clock(datetime(2026, 10, 14, 10, 0, tzinfo=KST))
    sim = SimBroker("real", clock=clock)
    sim.set_quote("US:BRK/B", D("480.10"))
    app = CentralApp(load_settings(home), db=Database.memory(), brokers={"real": sim, "paper": SimBroker("paper")},
                     clock=clock, realtime={})
    app.catalog.bulk_load([MasterItem("US", "BRK/B", "NYSE", "버크셔", "BERKSHIRE", "stock")])
    api = create_app(app, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test/api/v1") as http:
        days = (await http.get("/market/calendar", headers=ADMIN,
                               params={"market": "US", "from": "2026-12-24", "to": "2026-12-25"})).json()["data"]["days"]
        assert [(d["date"], d["open"], d.get("early_close_et")) for d in days] == [
            ("2026-12-24", True, "13:00"), ("2026-12-25", False, None)]
        q = (await http.get("/market/quote/US:BRK/B", headers=ADMIN)).json()
        assert q["ok"] and q["data"]["symbol"] == "US:BRK/B" and q["data"]["last"] == "480.10"
    await app.http.aclose()
