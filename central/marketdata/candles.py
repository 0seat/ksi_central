"""캔들 조회와 캐시 (docs/PLAN.md 7.2).

- 기본 봉: 1m, 1d (KIS 에서 받아 SQLite 에 캐시), 1w, 1M (매번 KIS 조회)
- 5m/15m/30m/60m 은 1m 을 장 시작 시각 기준으로 묶어 만든다 (국내 09:00 KST, 미국 09:30 ET)
- 완성된 봉만 캐시하고, 빠짐없이 받은 구간은 candle_coverage 에 기록해 다시 조회하지 않는다
- 한 요청에서 KIS 를 너무 많이 부르지 않도록 페이지 수를 제한한다 (넘으면 truncated)
- 봉 시각은 시작 시각 (KIS 실측: 15:30 봉이 종가 단일가, 미국 마지막 봉 19:59 ET)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Callable

from sqlalchemy import delete, select

from central.broker.base import Broker, Candle
from central.core.instruments import Instrument
from central.core.market_hours import KST, NEW_YORK, now_kst
from central.oms.errors import OmsError
from central.oms.queries import kst
from central.storage.db import Database
from central.storage.models import CandleCoverageRow, CandleRow

log = logging.getLogger(__name__)

MINUTE_INTERVALS = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}
INTERVALS = (*MINUTE_INTERVALS, "1d", "1w", "1M")
CACHED = ("1m", "1d")
MAX_PAGES = {"1m": 30, "1d": 10, "1w": 5, "1M": 5}
_KR_OPEN, _KR_LAST_BAR = time(9, 0), time(15, 30)


@dataclass(frozen=True)
class CandleResult:
    symbol: str
    interval: str
    candles: list[Candle]
    truncated: bool       # 페이지 제한에 걸려 start 까지 다 받지 못함
    kis_calls: int


class CandleService:
    def __init__(self, db: Database, broker_for: Callable[[str], Broker], clock: Callable[[], datetime] = now_kst) -> None:
        self.db, self.broker_for, self.clock = db, broker_for, clock

    async def get(self, inst: Instrument, interval: str, start: datetime, end: datetime) -> CandleResult:
        if interval not in INTERVALS:
            raise OmsError("INVALID_REQUEST", f"interval 은 {INTERVALS} 중 하나")
        start, end = start.astimezone(KST), min(end, self.clock()).astimezone(KST)
        if start > end:
            raise OmsError("INVALID_REQUEST", "from 이 to 보다 늦음")
        base = "1m" if interval in MINUTE_INTERVALS else interval
        bars, truncated, calls = await self._load(inst, base, start, end)
        if interval in MINUTE_INTERVALS and interval != "1m":
            bars = aggregate(bars, MINUTE_INTERVALS[interval], inst.market)
        return CandleResult(inst.id, interval, bars, truncated, calls)

    # ───────────── 내부 ─────────────
    def _cutoff(self, inst: Instrument, base: str) -> datetime:
        """이 시각보다 이른 봉은 완성된 봉."""
        now = self.clock().astimezone(KST)
        if base == "1m":
            return now.replace(second=0, microsecond=0) - timedelta(minutes=1)
        if inst.market == "US":
            ny_today = now.astimezone(NEW_YORK).date()
            return datetime.combine(ny_today, time.min, KST)
        return datetime.combine(now.date(), time.min, KST)

    async def _load(self, inst: Instrument, base: str, start: datetime, end: datetime) -> tuple[list[Candle], bool, int]:
        cutoff = self._cutoff(inst, base)
        lo = _floor_period(start, base)
        fetch_from = start
        if base in CACHED:
            covered_to = self._covered_to(inst.id, base, start)
            if covered_to is not None and covered_to >= end and end < cutoff:
                return self._from_db(inst.id, base, start, end), False, 0  # 전부 캐시에 있음
            if covered_to is not None:
                fetch_from = covered_to  # 캐시 끝부터 최신 쪽만 받는다

        broker = self.broker_for(inst.market)
        step = timedelta(minutes=1) if base == "1m" else timedelta(days=1)
        fetched: dict[datetime, Candle] = {}
        page_end, calls, reached, exhausted = _page_end(inst, base, end), 0, False, False
        while calls < MAX_PAGES[base]:
            page = await broker.candle_page(inst, base, page_end)
            calls += 1
            new = [b for b in page if b.ts not in fetched]
            if not new:
                exhausted = True
                break
            for b in page:
                fetched[b.ts] = b
            oldest = page[0].ts
            if oldest <= fetch_from:
                reached = True
                break
            page_end = _page_end(inst, base, oldest - step)
        truncated = not (reached or exhausted)
        if base == "1m":
            # 페이지 진행은 원본 봉 기준으로 판단했고, 저장과 응답은 정규장 봉만
            fetched = {ts: b for ts, b in fetched.items() if in_regular_session(inst.market, ts)}

        if base in CACHED:
            closed = [b for b in fetched.values() if b.ts < cutoff]
            self._save(inst.id, base, closed)
            if not truncated:
                self._cover(inst.id, base, start, min(end, cutoff))
            rows = {b.ts: b for b in self._from_db(inst.id, base, start, end)}
            rows.update({b.ts: b for b in fetched.values() if b.ts >= cutoff and lo <= b.ts <= end})
            bars = sorted(rows.values(), key=lambda b: b.ts)
        else:
            bars = sorted((b for b in fetched.values() if lo <= b.ts <= end), key=lambda b: b.ts)
        return bars, truncated, calls

    def _covered_to(self, symbol: str, base: str, start: datetime) -> datetime | None:
        with self.db.session() as s:
            ends = list(s.scalars(select(CandleCoverageRow.end).where(
                CandleCoverageRow.symbol == symbol, CandleCoverageRow.interval == base,
                CandleCoverageRow.start <= start, CandleCoverageRow.end >= start)))
        return kst(max(ends)) if ends else None

    def _cover(self, symbol: str, base: str, start: datetime, end: datetime) -> None:
        if end < start:
            return
        with self.db.session() as s, s.begin():
            # 새 구간 안에 들어가는 기존 기록은 지워 쌓이지 않게 한다
            s.execute(delete(CandleCoverageRow).where(
                CandleCoverageRow.symbol == symbol, CandleCoverageRow.interval == base,
                CandleCoverageRow.start >= start, CandleCoverageRow.end <= end))
            s.add(CandleCoverageRow(symbol=symbol, interval=base, start=start, end=end, fetched_at=self.clock()))

    def _save(self, symbol: str, base: str, bars: list[Candle]) -> None:
        if not bars:
            return
        with self.db.session() as s, s.begin():
            for b in bars:
                s.merge(CandleRow(symbol=symbol, interval=base, ts=b.ts, open=b.open, high=b.high, low=b.low,
                                  close=b.close, volume=b.volume))

    def _from_db(self, symbol: str, base: str, start: datetime, end: datetime) -> list[Candle]:
        lo = _floor_period(start, base)
        with self.db.session() as s:
            rows = s.scalars(select(CandleRow).where(
                CandleRow.symbol == symbol, CandleRow.interval == base, CandleRow.ts >= lo, CandleRow.ts <= end,
            ).order_by(CandleRow.ts))
            out = [Candle(kst(r.ts), r.open, r.high, r.low, r.close, r.volume) for r in rows]  # type: ignore[arg-type]
        # 정규장 필터가 생기기 전에 캐시된 시간외 봉은 응답에서 뺀다
        return [b for b in out if in_regular_session(symbol.split(":")[0], b.ts)] if base == "1m" else out


def _floor_period(t: datetime, base: str) -> datetime:
    """일봉 이상은 날짜 단위로 비교한다 (봉 시각이 거래일 0시)."""
    return t if base == "1m" else datetime.combine(t.date(), time.min, KST)


def _page_end(inst: Instrument, base: str, t: datetime) -> datetime:
    """국내 1분봉 요청 시각을 정규장 마지막 봉(15:30) 이하로 맞춘다 (2026-10-15 실측).

    - 장 시작 전 시각: KIS 영업일이 아직 넘어가지 않아 하루를 건너뛴 봉이 온다
      (10-15 08:00 요청 → 10-13 봉). 전날 15:30 으로 요청한다. 휴일/주말의 15:30 은 직전 거래일 봉이 온다.
    - 15:30 이후 시각: 시간외 봉(~19:59)이 먼저 와서 페이지를 낭비한다. 그날 15:30 으로 요청한다.
    """
    if base == "1m" and inst.market == "KR":
        t = t.astimezone(KST)
        if t.time() < _KR_OPEN:
            return datetime.combine(t.date() - timedelta(days=1), _KR_LAST_BAR, KST)
        if t.time() > _KR_LAST_BAR:
            return datetime.combine(t.date(), _KR_LAST_BAR, KST)
    return t


def in_regular_session(market: str, ts: datetime) -> bool:
    """분봉 시작 시각이 정규장 안인지. 국내 09:00~15:30(종가 단일가 봉 포함), 미국 09:30~15:59 ET."""
    if market == "KR":
        t = ts.astimezone(KST).time()
        return _KR_OPEN <= t <= _KR_LAST_BAR
    t = ts.astimezone(NEW_YORK).time()
    return time(9, 30) <= t < time(16, 0)


def _session_open(ts: datetime, market: str) -> datetime:
    if market == "KR":
        return datetime.combine(ts.astimezone(KST).date(), _KR_OPEN, KST)
    local = ts.astimezone(NEW_YORK)
    return datetime.combine(local.date(), time(9, 30), NEW_YORK)


def aggregate(bars: list[Candle], minutes: int, market: str) -> list[Candle]:
    """1분봉을 N분봉으로. 구간은 장 시작 시각부터 N분 단위, 봉 시각은 구간 시작."""
    groups: dict[datetime, list[Candle]] = {}
    for b in bars:
        open_ = _session_open(b.ts, market)
        idx = max(0, int((b.ts - open_).total_seconds() // 60) // minutes)
        key = (open_ + timedelta(minutes=idx * minutes)).astimezone(KST)
        groups.setdefault(key, []).append(b)
    out = []
    for key in sorted(groups):
        g = groups[key]
        out.append(Candle(key, g[0].open, max(x.high for x in g), min(x.low for x in g), g[-1].close,
                          sum(x.volume for x in g)))
    return out
