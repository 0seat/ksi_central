"""영업일 캘린더 (docs/PLAN.md 7.2).

국내: KIS 휴장일 조회(CTCA0903R, 실전 전용)를 하루 한 번 받아 market_days 에 저장한다 (공식 권장 1일 1회).
      1회 24일치, ctx_area_nk 로 이어 받는다 (2026-10-14 실측). 저장되지 않은 날은 평일=개장으로 추정한다.
미국: NYSE 규칙으로 계산한다. market_days 에 US 행이 있으면 그 값이 우선이다 (임시 휴장 등 수동 반영).
  - 휴장: 신정(토요일이면 대체 없음, 일요일이면 월요일), MLK(1월 셋째 월), 대통령의 날(2월 셋째 월),
    성금요일, 메모리얼(5월 마지막 월), 준틴스(6/19, 2022~), 독립기념일(7/4), 노동절(9월 첫 월),
    추수감사절(11월 넷째 목), 성탄절(12/25). 토요일이면 금요일, 일요일이면 월요일에 대체
  - 조기폐장 13:00 ET: 독립기념일 전날(7/3 이 거래일일 때), 추수감사절 다음 날, 성탄 전날(12/24 이 거래일일 때)
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Callable

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from central.broker.base import Broker
from central.core.market_hours import KST, MarketHours, now_kst
from central.storage.db import Database
from central.storage.models import MarketDayRow

log = logging.getLogger(__name__)

US_EARLY_CLOSE = time(13, 0)


# ───────────── 미국 규칙 ─────────────
def _easter(year: int) -> date:
    """그레고리력 부활절 (Anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month = (h + m - 7 * n + 114) // 31
    day = (h + m - 7 * n + 114) % 31 + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    return d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d


def us_holidays(year: int) -> dict[date, str]:
    out: dict[date, str] = {}
    ny = date(year, 1, 1)
    if ny.weekday() != 5:                       # 토요일 신정은 전년 12/31 로 대체하지 않는다 (NYSE)
        out[_observed(ny)] = "New Year's Day"
    out[_nth_weekday(year, 1, 0, 3)] = "Martin Luther King Jr. Day"
    out[_nth_weekday(year, 2, 0, 3)] = "Washington's Birthday"
    out[_easter(year) - timedelta(days=2)] = "Good Friday"
    out[_last_weekday(year, 5, 0)] = "Memorial Day"
    if year >= 2022:
        out[_observed(date(year, 6, 19))] = "Juneteenth"
    out[_observed(date(year, 7, 4))] = "Independence Day"
    out[_nth_weekday(year, 9, 0, 1)] = "Labor Day"
    out[_nth_weekday(year, 11, 3, 4)] = "Thanksgiving Day"
    out[_observed(date(year, 12, 25))] = "Christmas Day"
    return out


def us_early_closes(year: int) -> dict[date, time]:
    hol = us_holidays(year)
    out: dict[date, time] = {}
    for d in (date(year, 7, 3), _nth_weekday(year, 11, 3, 4) + timedelta(days=1), date(year, 12, 24)):
        if d.weekday() < 5 and d not in hol:
            out[d] = US_EARLY_CLOSE
    return out


# ───────────── 서비스 ─────────────
class CalendarService:
    def __init__(self, db: Database, hours: MarketHours, kr_source: Callable[[], Broker | None],
                 clock: Callable[[], datetime] = now_kst) -> None:
        self.db, self.hours, self.kr_source, self.clock = db, hours, kr_source, clock
        self._kr_refreshed: date | None = None

    async def refresh_kr(self, pages: int = 5, force: bool = False) -> int:
        """국내 휴장일을 KIS 에서 받아 저장 (하루 한 번). 이번 달 1일부터 약 120일. 받은 날짜 수를 돌려준다."""
        today = self.clock().astimezone(KST).date()
        if not force and (self._kr_refreshed == today or self._fetched_on("KR") == today):
            self._kr_refreshed = today
            return 0
        broker = self.kr_source()
        if broker is None:
            log.warning("국내 휴장일을 조회할 실전 계좌 없음 (모의는 CTCA0903R 미지원). 평일=개장으로 추정")
            return 0
        days = await broker.market_days(today.replace(day=1), pages)
        now = self.clock()
        rows = [{"market": "KR", "day": d, "open": o, "fetched_at": now} for d, o in days]
        if rows:
            with self.db.session() as s, s.begin():
                stmt = insert(MarketDayRow).values(rows)
                s.execute(stmt.on_conflict_do_update(index_elements=["market", "day"], set_={
                    "open": stmt.excluded.open, "fetched_at": stmt.excluded.fetched_at}))
        self._kr_refreshed = today
        log.info("국내 개장일 %d일 갱신 (%s ~ %s)", len(rows), rows[0]["day"] if rows else "-", rows[-1]["day"] if rows else "-")
        self.apply()
        return len(rows)

    def apply(self) -> None:
        """저장된 휴장일과 미국 규칙을 MarketHours 에 반영 (주문 허용 판단에 쓰인다)."""
        year = self.clock().year
        stored = self._stored()
        self.hours.holidays["KR"] = {d for (m, d), o in stored.items() if m == "KR" and not o}
        us: set[date] = set()
        early: dict[date, time] = {}
        for y in (year - 1, year, year + 1):
            us |= set(us_holidays(y))
            early.update(us_early_closes(y))
        for (m, d), o in stored.items():
            if m == "US":
                (us.discard if o else us.add)(d)
        self.hours.holidays["US"] = us
        self.hours.us_early_close = early

    def days(self, market: str, start: date, end: date) -> list[dict]:
        stored = self._stored()
        out = []
        d = start
        while d <= end:
            if (market, d) in stored:
                is_open, source = stored[(market, d)], "kis" if market == "KR" else "manual"
            elif market == "US":
                is_open, source = d.weekday() < 5 and d not in us_holidays(d.year), "rule"
            else:
                is_open, source = d.weekday() < 5, "weekday"   # KIS 에서 아직 받지 않은 날
            item = {"date": d, "open": is_open, "source": source}
            if market == "US" and is_open and d in us_early_closes(d.year):
                item["early_close_et"] = US_EARLY_CLOSE.strftime("%H:%M")
            out.append(item)
            d += timedelta(days=1)
        return out

    def _stored(self) -> dict[tuple[str, date], bool]:
        with self.db.session() as s:
            return {(r.market, r.day): r.open for r in s.scalars(select(MarketDayRow))}

    def _fetched_on(self, market: str) -> date | None:
        with self.db.session() as s:
            last = s.scalar(select(MarketDayRow.fetched_at).where(MarketDayRow.market == market)
                            .order_by(MarketDayRow.fetched_at.desc()).limit(1))
        return last.date() if last else None
