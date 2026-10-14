"""과거 시세 보관소: 원주가 저장 + 기업행위 이벤트로 수정주가 계산.

KIS 는 1분봉을 국내 약 1년, 미국 약 1개월만 보관하고 지수 1분봉은 최근 약 100개만 준다 (2026-10-14/15 실측).
매일 받아 쌓아 두면 KIS 에서 사라진 뒤에도 쓸 수 있다.

수정주가 원칙
- 저장은 원주가(수정하지 않은 가격)만 한다. 원주가는 나중에 바뀌지 않는다.
- 분할, 병합, 무상증자, 배당은 이벤트 (기준일, 비율 k, 종류) 로 따로 저장하고 읽을 때 곱한다.
  기준일 '이전' 가격에 k 를 곱하면 수정주가가 된다 (예: 4:1 분할 k=1/4, 1:8 병합 k=8).
- 이벤트는 KIS 원주가 일봉과 수정주가 일봉의 비율(수정/원)이 계단처럼 바뀌는 날에서 찾는다.
  국내 수정주가(FID_ORG_ADJ_PRC=0)는 자본변동(분할, 병합, 무상증자 등)만,
  미국 수정주가(MODP=1)는 분할과 배당을 함께 반영한다 (2026-10-14 실측).
- 수정 방식: none(원주가), split(분할/병합 등 자본변동만), total(배당까지. 국내는 split 과 같다)
- 거래량은 자본변동 비율로만 나눈다 (배당은 거래량과 무관).

저장 위치 (CENTRAL_HOME/data/history)
  <KR|US>/<코드>/1m/<YYYY-MM>.parquet    1분봉 원본 (시간외 포함). 월은 현지 거래일 기준
  <KR|US>/<코드>/1d.parquet              원주가 일봉
  <KR|US>/<코드>/events.json             기업행위 이벤트
  INDEX_KR/<코드>/..., INDEX_US/<코드>/... 지수 (수정 없음). 키 예: INDEX:KR:0001, INDEX:US:COMP
"""

from __future__ import annotations

import json
import logging
import os
import statistics
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

import pyarrow as pa
import pyarrow.parquet as pq

from central.broker.base import Candle
from central.core.instruments import Instrument
from central.core.market_hours import KST, NEW_YORK, now_kst
from central.marketdata.candles import _page_end, in_regular_session

log = logging.getLogger(__name__)

Adjust = Literal["none", "split", "total"]
ADJUST_MODES: tuple[str, ...] = ("none", "split", "total")
PRICE_QUANT = Decimal("0.0001")

_MINUTE_SCHEMA = pa.schema([("ts", pa.timestamp("us", tz="UTC")), ("open", pa.string()), ("high", pa.string()),
                            ("low", pa.string()), ("close", pa.string()), ("volume", pa.int64())])
_DAILY_SCHEMA = pa.schema([("date", pa.date32()), ("open", pa.string()), ("high", pa.string()),
                           ("low", pa.string()), ("close", pa.string()), ("volume", pa.int64())])


# ───────────── 키 ─────────────
@dataclass(frozen=True)
class SeriesKey:
    """보관 단위. 종목은 KR:005930 / US:AAPL, 지수는 INDEX:KR:0001 / INDEX:US:COMP."""
    market: str          # KR | US
    code: str
    index: bool = False

    @classmethod
    def parse(cls, key: str) -> SeriesKey:
        parts = key.strip().upper().split(":")
        if len(parts) == 3 and parts[0] == "INDEX" and parts[1] in ("KR", "US") and parts[2]:
            return cls(parts[1], parts[2], True)
        if len(parts) == 2 and parts[0] in ("KR", "US") and parts[1]:
            return cls(parts[0], parts[1])
        raise ValueError(f"보관 키 형식 오류: {key} (예: KR:005930, US:AAPL, INDEX:KR:0001, INDEX:US:COMP)")

    def __str__(self) -> str:
        return f"INDEX:{self.market}:{self.code}" if self.index else f"{self.market}:{self.code}"

    @property
    def zone(self):
        """거래일을 정하는 시간대 (봉 날짜, 이벤트 기준일 비교에 쓴다)."""
        return KST if self.market == "KR" else NEW_YORK

    @property
    def folder(self) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in self.code)   # 예: .DJI → _DJI
        return Path(f"INDEX_{self.market}" if self.index else self.market) / safe


# ───────────── 이벤트 ─────────────
@dataclass(frozen=True)
class Event:
    date: date        # 기준일. 이 날짜 '이전' 봉에 ratio 를 곱한다
    ratio: Decimal    # k: 4:1 분할 0.25, 1:8 병합 8
    kind: str         # split (자본변동) | dividend (배당 등, 미국만)

    def to_json(self) -> dict:
        return {"date": self.date.isoformat(), "ratio": str(self.ratio), "kind": self.kind}

    @classmethod
    def from_json(cls, d: dict) -> Event:
        return cls(date.fromisoformat(d["date"]), Decimal(d["ratio"]), d["kind"])


# KIS 가격 단위: 비율 비교 허용오차를 정할 때 쓴다 (국내 원 단위, 미국 소수 4자리)
_UNIT = {"KR": Decimal("1"), "US": Decimal("0.0001")}


def _tol(market: str, raw: Decimal, adj: Decimal) -> float:
    u = _UNIT[market]
    return float(2 * (u / adj + u / raw)) + 1e-6


def _snap(k: Decimal, limit: int = 100) -> Fraction | None:
    """분할/병합 비율이면 단순 분수로 (예: 0.24999 → 1/4). 0.5% 안에 맞는 분수가 없으면 None."""
    f = Fraction(str(k)).limit_denominator(limit)
    if f.numerator > limit or f.numerator <= 0:
        return None
    return f if abs(float(f) / float(k) - 1) <= 0.005 else None


def detect_events(raw: list[Candle], adj: list[Candle], market: str) -> list[Event]:
    """원주가/수정주가 일봉의 종가 비율이 바뀌는 날을 이벤트로. 하루짜리 튐(KIS 자료 오류)은 무시한다.

    비율 r = 수정/원 은 이벤트 사이에서 일정하고, 가장 최근 구간은 1 이다.
    k = (이전 구간의 r) / (다음 구간의 r). 국내는 모두 자본변동, 미국은 단순 분수면 분할/병합, 아니면 배당.
    """
    a = {b.ts.date(): b.close for b in adj if b.close > 0}
    pts = [(b.ts.date(), b.close, a[b.ts.date()]) for b in sorted(raw, key=lambda x: x.ts)
           if b.close > 0 and b.volume > 0 and b.ts.date() in a]
    if len(pts) < 2:
        return []
    r = [float(p[2] / p[1]) for p in pts]
    tol = [_tol(market, p[1], p[2]) for p in pts]

    def same(i: int, level: float, ltol: float) -> bool:
        return abs(r[i] / level - 1) <= tol[i] + ltol

    # 최근부터 과거로 구간을 나눈다. 구간 기준값은 경계 가까운 최근 15개의 중앙값
    segments: list[list[int]] = []
    cur = [len(pts) - 1]
    i = len(pts) - 2
    while i >= 0:
        recent = cur[-15:]
        level = statistics.median(r[j] for j in recent)
        ltol = max(tol[j] for j in recent)
        if same(i, level, ltol):
            cur.append(i)
        elif i - 1 >= 0 and same(i - 1, level, ltol):
            pass   # 하루짜리 튐: 건너뛴다
        else:
            segments.append(cur)
            cur = [i]
        i -= 1
    segments.append(cur)

    events: list[Event] = []
    for newer, older in zip(segments, segments[1:]):
        hi = Decimal(str(statistics.median(r[j] for j in newer)))
        lo = Decimal(str(statistics.median(r[j] for j in older)))
        k = (lo / hi).quantize(Decimal("1.0000000000")).normalize()
        ex_date = pts[min(newer)][0]
        frac = _snap(k) if abs(float(k) - 1) > 0.05 else None
        if frac is not None:
            events.append(Event(ex_date, (Decimal(frac.numerator) / Decimal(frac.denominator))
                                .quantize(Decimal("1.0000000000")).normalize(), "split"))
        else:
            # 국내 수정주가는 배당을 반영하지 않으므로 모두 자본변동. 미국은 단순 분수가 아니면 배당 등(total 에만 적용)
            events.append(Event(ex_date, k, "split" if market == "KR" else "dividend"))
    return sorted(events, key=lambda e: e.date)


def factor_table(events: Iterable[Event], mode: Adjust) -> tuple[list[date], list[Decimal], list[Decimal]]:
    """(기준일 오름차순, 그 기준일 '이전' 가격 계수, 거래량 계수). 기준일 이후는 계수 1."""
    if mode == "none":
        return [], [], []
    evs = sorted((e for e in events if mode == "total" or e.kind == "split"), key=lambda e: e.date)
    dates, price, vol = [], [], []
    p = v = Decimal(1)
    for e in reversed(evs):
        p *= e.ratio
        if e.kind == "split":
            v *= e.ratio
        dates.append(e.date)
        price.append(p)
        vol.append(v)
    return dates[::-1], price[::-1], vol[::-1]


def adjust(bars: list[Candle], events: Iterable[Event], mode: Adjust, zone) -> list[Candle]:
    """봉 목록에 이벤트를 적용. 봉의 거래일(zone 기준)이 기준일보다 이르면 계수를 곱한다."""
    if mode not in ADJUST_MODES:
        raise ValueError(f"adjust 는 {ADJUST_MODES} 중 하나")
    dates, price, vol = factor_table(events, mode)
    if not dates:
        return list(bars)
    out = []
    for b in bars:
        d = b.ts.astimezone(zone).date()
        # d 보다 늦은 첫 기준일의 누적 계수
        idx = next((n for n, x in enumerate(dates) if x > d), None)
        if idx is None:
            out.append(b)
            continue
        f, fv = price[idx], vol[idx]
        out.append(Candle(b.ts, (b.open * f).quantize(PRICE_QUANT), (b.high * f).quantize(PRICE_QUANT),
                          (b.low * f).quantize(PRICE_QUANT), (b.close * f).quantize(PRICE_QUANT),
                          int((Decimal(b.volume) / fv).to_integral_value())))
    return out


def reconstruction_error(raw: list[Candle], adj: list[Candle], events: list[Event], market: str) -> dict:
    """원주가에 이벤트를 적용한 값과 KIS 수정주가의 차이 (검증용). 허용오차 밖인 날 수와 최대 상대오차."""
    mine = {b.ts.date(): b.close for b in adjust(raw, events, "total", KST)}
    worst, bad, n = 0.0, 0, 0
    for b in adj:
        d = b.ts.date()
        if d not in mine or b.close <= 0:
            continue
        n += 1
        rel = abs(float(mine[d] / b.close) - 1)
        worst = max(worst, rel)
        if rel > float(_UNIT[market] / b.close) * 2 + 1e-4:
            bad += 1
    return {"days": n, "worst": worst, "days_off": bad}


# ───────────── 정규장 판정 ─────────────
_KR_LATE = (time(10, 0), time(16, 30))   # 수능일: 1시간 늦게 열고 늦게 닫는다 (2025-11-13 실측 10:00 개장)


def _kr_late_day(day: date, bars: Iterable[Candle]) -> bool:
    """11월에 09시대 봉 없이 10시에 시작한 날 = 수능일. (1월 첫 거래일도 10시 개장이지만 마감은 15:30)"""
    if day.month != 11:
        return False
    times = [b.ts.astimezone(KST).time() for b in bars]
    return bool(times) and not any(time(9, 0) <= t < time(10, 0) for t in times) and any(
        time(10, 0) <= t <= time(10, 5) for t in times)


def regular_only(key: SeriesKey, bars: list[Candle]) -> list[Candle]:
    """정규장 봉만. 국내 09:00~15:30 (수능일 10:00~16:30), 미국 09:30~15:59 ET. 지수는 거르지 않는다."""
    if key.index:
        return bars
    if key.market != "KR":
        return [b for b in bars if in_regular_session("US", b.ts)]
    by_day: dict[date, list[Candle]] = {}
    for b in bars:
        by_day.setdefault(b.ts.astimezone(KST).date(), []).append(b)
    out = []
    for day, group in by_day.items():
        if _kr_late_day(day, group):
            out += [b for b in group if _KR_LATE[0] <= b.ts.astimezone(KST).time() <= _KR_LATE[1]]
        else:
            out += [b for b in group if in_regular_session("KR", b.ts)]
    return sorted(out, key=lambda b: b.ts)


# ───────────── 저장소 ─────────────
def _write(path: Path, table: pa.Table) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    pq.write_table(table, tmp)
    os.replace(tmp, path)


def _to_candles_minute(table: pa.Table) -> list[Candle]:
    d = table.to_pydict()
    return [Candle(ts.astimezone(KST), Decimal(o), Decimal(h), Decimal(lo), Decimal(c), v)
            for ts, o, h, lo, c, v in zip(d["ts"], d["open"], d["high"], d["low"], d["close"], d["volume"])]


class HistoryStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _base(self, key: SeriesKey) -> Path:
        return self.root / key.folder

    # 1분봉
    def save_minutes(self, key: SeriesKey, bars: Iterable[Candle]) -> int:
        """월별 파일에 합친다 (같은 시각은 새 값으로). 새로 생긴 봉 수를 돌려준다."""
        by_month: dict[str, dict[datetime, Candle]] = {}
        for b in bars:
            by_month.setdefault(b.ts.astimezone(key.zone).strftime("%Y-%m"), {})[b.ts] = b
        added = 0
        for month, new in by_month.items():
            path = self._base(key) / "1m" / f"{month}.parquet"
            old = {b.ts: b for b in _to_candles_minute(pq.read_table(path))} if path.exists() else {}
            added += len(set(new) - set(old))
            old.update(new)
            rows = sorted(old.values(), key=lambda b: b.ts)
            _write(path, pa.Table.from_pydict({
                "ts": [b.ts for b in rows], "open": [str(b.open) for b in rows], "high": [str(b.high) for b in rows],
                "low": [str(b.low) for b in rows], "close": [str(b.close) for b in rows],
                "volume": [int(b.volume) for b in rows]}, schema=_MINUTE_SCHEMA))
        return added

    def minutes(self, key: SeriesKey, start: datetime | None = None, end: datetime | None = None,
                regular: bool = True) -> list[Candle]:
        folder = self._base(key) / "1m"
        if not folder.exists():
            return []
        lo = start.astimezone(key.zone).strftime("%Y-%m") if start else ""
        hi = end.astimezone(key.zone).strftime("%Y-%m") if end else "9999"
        out: list[Candle] = []
        for path in sorted(folder.glob("*.parquet")):
            if lo <= path.stem <= hi:
                out += _to_candles_minute(pq.read_table(path))
        out = [b for b in out if (start is None or b.ts >= start) and (end is None or b.ts <= end)]
        return regular_only(key, out) if regular else out

    def last_minute(self, key: SeriesKey) -> datetime | None:
        folder = self._base(key) / "1m"
        files = sorted(folder.glob("*.parquet")) if folder.exists() else []
        if not files:
            return None
        bars = _to_candles_minute(pq.read_table(files[-1]))
        return max(b.ts for b in bars) if bars else None

    # 일봉 (원주가)
    def save_daily(self, key: SeriesKey, bars: Iterable[Candle]) -> int:
        path = self._base(key) / "1d.parquet"
        old = {b.ts.date(): b for b in self.daily(key)}
        new = {b.ts.date(): b for b in bars}
        added = len(set(new) - set(old))
        old.update(new)
        rows = [old[d] for d in sorted(old)]
        _write(path, pa.Table.from_pydict({
            "date": [b.ts.date() for b in rows], "open": [str(b.open) for b in rows],
            "high": [str(b.high) for b in rows], "low": [str(b.low) for b in rows],
            "close": [str(b.close) for b in rows], "volume": [int(b.volume) for b in rows]}, schema=_DAILY_SCHEMA))
        return added

    def daily(self, key: SeriesKey, start: date | None = None, end: date | None = None) -> list[Candle]:
        path = self._base(key) / "1d.parquet"
        if not path.exists():
            return []
        d = pq.read_table(path).to_pydict()
        out = [Candle(datetime.combine(day, time.min, KST), Decimal(o), Decimal(h), Decimal(lo), Decimal(c), v)
               for day, o, h, lo, c, v in zip(d["date"], d["open"], d["high"], d["low"], d["close"], d["volume"])]
        return [b for b in out if (start is None or b.ts.date() >= start) and (end is None or b.ts.date() <= end)]

    # 이벤트
    def events(self, key: SeriesKey) -> list[Event]:
        path = self._base(key) / "events.json"
        if not path.exists():
            return []
        return [Event.from_json(x) for x in json.loads(path.read_text(encoding="utf-8"))["events"]]

    def save_events(self, key: SeriesKey, events: list[Event], checked_at: datetime) -> None:
        path = self._base(key) / "events.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"symbol": str(key), "checked_at": checked_at.isoformat(),
                                   "events": [e.to_json() for e in sorted(events, key=lambda e: e.date)]},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    def read(self, key: SeriesKey, interval: str, start: datetime | None, end: datetime | None,
             mode: Adjust = "none") -> list[Candle]:
        """보관본 조회 + 수정 적용. interval: 1m (정규장만) | 1d."""
        if interval == "1d":
            bars = self.daily(key, start.astimezone(key.zone).date() if start else None,
                              end.astimezone(key.zone).date() if end else None)
            zone = KST   # 일봉 ts 는 거래일 0시 KST 로 저장
        elif interval == "1m":
            bars, zone = self.minutes(key, start, end), key.zone
        else:
            raise ValueError("보관본 interval 은 1m, 1d")
        return bars if key.index or mode == "none" else adjust(bars, self.events(key), mode, zone)

    def symbols(self) -> list[str]:
        out = []
        for market in ("KR", "US", "INDEX_KR", "INDEX_US"):
            folder = self.root / market
            if folder.exists():
                prefix = market.replace("_", ":") if market.startswith("INDEX") else market
                out += [f"{prefix}:{p.name}" for p in sorted(folder.iterdir()) if p.is_dir()]
        return out


# ───────────── 가져오기 ─────────────
class HistoryFetcher:
    """KIS 에서 받아 보관소에 쌓는다. broker_for(market) 는 history_minutes/history_daily/index_* 가 있는 브로커."""

    def __init__(self, store: HistoryStore, broker_for: Callable[[str], Any],
                 clock: Callable[[], datetime] = now_kst, max_pages: int = 3000) -> None:
        self.store, self.broker_for, self.clock, self.max_pages = store, broker_for, clock, max_pages

    async def minutes(self, inst: Instrument, progress: Callable[[str], None] | None = None) -> dict:
        """보관본의 마지막 봉까지(없으면 KIS 가 주는 끝까지) 과거로 받는다. 진행 중인 봉은 저장하지 않는다."""
        key = SeriesKey(inst.market, inst.code)
        broker = self.broker_for(inst.market)
        stop_at = self.store.last_minute(key)
        cutoff = self.clock().replace(second=0, microsecond=0) - timedelta(minutes=1)
        end = _page_end(inst, "1m", self.clock())
        seen: dict[datetime, Candle] = {}
        calls = 0
        while calls < self.max_pages:
            page = await broker.history_minutes(inst, end)
            calls += 1
            new = [b for b in page if b.ts not in seen]
            if not new:
                break
            for b in new:
                seen[b.ts] = b
            oldest = page[0].ts
            if progress and calls % 100 == 0:
                progress(f"{key} 1분봉 {calls}회, {oldest:%Y-%m-%d}")
            if stop_at is not None and oldest <= stop_at:
                break
            end = _page_end(inst, "1m", oldest - timedelta(minutes=1))
        if inst.market == "KR":
            # 요청 시각을 15:30 으로 맞추므로 수능일의 15:31~16:30 은 따로 한 페이지 더 받는다
            by_day: dict[date, list[Candle]] = {}
            for b in seen.values():
                by_day.setdefault(b.ts.astimezone(KST).date(), []).append(b)
            for day, group in by_day.items():
                if _kr_late_day(day, group):
                    page = await broker.history_minutes(inst, datetime.combine(day, _KR_LATE[1], KST))
                    calls += 1
                    seen.update({b.ts: b for b in page})
        added = self.store.save_minutes(key, (b for b in seen.values() if b.ts < cutoff))
        return {"symbol": str(key), "calls": calls, "added": added,
                "oldest": min(seen).isoformat() if seen else None, "truncated": calls >= self.max_pages}

    def _today(self, key: SeriesKey) -> date:
        return self.clock().astimezone(key.zone).date()

    async def _pages(self, fetch: Callable[[date], Any], stop: date | None, today: date) -> tuple[list[Candle], int]:
        """일봉 페이지를 과거로 넘기며 받는다. stop 이 있으면 그 날짜에 닿으면 멈춘다.
        현지 기준 오늘 봉은 아직 바뀔 수 있어 받지 않는다 (원주가와 수정주가를 받는 사이에 값이 달라져 가짜 이벤트가 생김)."""
        got: dict[date, Candle] = {}
        end, calls = today - timedelta(days=1), 0
        while calls < self.max_pages:
            page = await fetch(end)
            calls += 1
            new = [b for b in page if b.ts.date() not in got]
            if not new:
                break
            for b in new:
                got[b.ts.date()] = b
            oldest = min(b.ts.date() for b in new)
            if stop is not None and oldest <= stop:
                break
            end = oldest - timedelta(days=1)
        return [got[d] for d in sorted(got)], calls

    async def daily(self, inst: Instrument, full: bool = False) -> dict:
        """원주가 일봉과 이벤트 갱신. 처음(또는 full)이면 전체, 이후에는 최근 구간만 받아 새 이벤트를 찾는다."""
        key = SeriesKey(inst.market, inst.code)
        broker = self.broker_for(inst.market)
        stored = self.store.daily(key)
        events = self.store.events(key)
        last = stored[-1].ts.date() if stored and not full else None
        today = self._today(key)
        raw, c1 = await self._pages(lambda end: broker.history_daily(inst, end, adjusted=False), last, today)
        if last is None:
            adj, c2 = await self._pages(lambda end: broker.history_daily(inst, end, adjusted=True), None, today)
            events = detect_events(raw, adj, inst.market)
            window_raw = raw
        else:
            # 최근 한 페이지(약 100 거래일)의 비율 변화로 새 이벤트를 찾는다. 이미 아는 이벤트는 그대로 둔다
            adj, c2 = await self._pages(lambda end: broker.history_daily(inst, end, adjusted=True), today, today)
            window_raw = {b.ts.date(): b for b in stored} | {b.ts.date(): b for b in raw}
            window_raw = [window_raw[d] for d in sorted(window_raw) if d >= min(b.ts.date() for b in adj)] if adj else []
            known = {e.date for e in events}
            latest = max(known) if known else date.min
            for e in detect_events(window_raw, adj, inst.market):
                if e.date > latest:
                    events.append(e)
                    log.warning("%s 기업행위 감지: %s 이전 가격 x %s (%s)", key, e.date, e.ratio, e.kind)
        added = self.store.save_daily(key, raw)
        self.store.save_events(key, events, self.clock())
        check = reconstruction_error(window_raw, adj, events, inst.market) if adj else {}
        return {"symbol": str(key), "calls": c1 + c2, "added": added, "events": len(events), "check": check}

    async def index_daily(self, key: SeriesKey, full: bool = False) -> dict:
        broker = self.broker_for("INDEX")
        stored = self.store.daily(key)
        last = stored[-1].ts.date() if stored and not full else None
        bars, calls = await self._pages(lambda end: broker.index_daily(key.market, key.code, end), last, self._today(key))
        return {"symbol": str(key), "calls": calls, "added": self.store.save_daily(key, bars),
                "oldest": bars[0].ts.date().isoformat() if bars else None}

    async def index_minutes(self, key: SeriesKey) -> dict:
        broker = self.broker_for("INDEX")
        cutoff = self.clock().replace(second=0, microsecond=0) - timedelta(minutes=1)
        bars = [b for b in await broker.index_minutes(key.market, key.code) if b.ts < cutoff]
        return {"symbol": str(key), "calls": 1, "added": self.store.save_minutes(key, bars),
                "oldest": bars[0].ts.isoformat() if bars else None}
