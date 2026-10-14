"""틱 저장 (Parquet).

경로: {ticks_dir}/{시장}/{YYYYMMDD}/{코드}/part-{HHMMSSffffff}.parquet
Parquet 는 이어 쓰기가 안 되므로 flush 마다 조각 파일을 만들고, 장 마감 후 compact() 로 하나로 합친다.
가격은 decimal128(18,4) 로 저장해 소수 오차가 없다.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable

import pyarrow as pa
import pyarrow.parquet as pq

from central.core.market_hours import now_kst
from central.marketdata.realtime import Tick

log = logging.getLogger(__name__)

_PRICE = pa.decimal128(18, 4)
SCHEMA = pa.schema([
    ("ts", pa.timestamp("us", tz="Asia/Seoul")),
    ("price", _PRICE),
    ("qty", pa.int64()),
    ("cum_volume", pa.int64()),
    ("bid", _PRICE),
    ("ask", _PRICE),
    ("side", pa.string()),
    ("venue", pa.string()),
])
_Q = Decimal("0.0001")


def _q(v: Decimal | None) -> Decimal | None:
    return v.quantize(_Q) if v is not None else None


class TickStore:
    def __init__(self, root: Path, *, flush_seconds: float = 30.0, max_buffer: int = 20000,
                 clock: Callable[[], datetime] = now_kst) -> None:
        self.root, self.flush_seconds, self.max_buffer, self.clock = root, flush_seconds, max_buffer, clock
        self._buf: dict[tuple[str, str, str], list[Tick]] = defaultdict(list)
        self._count = 0
        self._task: asyncio.Task | None = None
        self.written = 0
        self.counts: Counter[tuple[str, str]] = Counter()   # (YYYYMMDD, 종목) → 받은 틱 수 (운영 보고용)

    def _dir(self, market: str, day: str, code: str) -> Path:
        return self.root / market / day / code

    # ───────────── 쓰기 ─────────────
    def on_market(self, channel: str, item: object) -> None:
        """MarketDataHub 리스너."""
        if isinstance(item, Tick):
            market, _, code = item.symbol.partition(":")
            day = item.ts.strftime("%Y%m%d")
            self._buf[(market, day, code)].append(item)
            self.counts[(day, item.symbol)] += 1
            self._count += 1
            if self._count >= self.max_buffer:
                self.flush()

    def flush(self) -> int:
        buf, self._buf, self._count = self._buf, defaultdict(list), 0
        n = 0
        for (market, day, code), ticks in buf.items():
            if not ticks:
                continue
            d = self._dir(market, day, code)
            d.mkdir(parents=True, exist_ok=True)
            table = pa.table({
                "ts": [t.ts for t in ticks],
                "price": [_q(t.price) for t in ticks],
                "qty": [t.qty for t in ticks],
                "cum_volume": [t.cum_volume for t in ticks],
                "bid": [_q(t.bid) for t in ticks],
                "ask": [_q(t.ask) for t in ticks],
                "side": [t.side for t in ticks],
                "venue": [t.venue for t in ticks],
            }, schema=SCHEMA)
            path = d / f"part-{self.clock().strftime('%H%M%S%f')}.parquet"
            while path.exists():  # 같은 마이크로초 충돌 방지
                path = path.with_name(path.stem + "x.parquet")
            pq.write_table(table, path)
            n += len(ticks)
        self.written += n
        return n

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="tick-store")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.flush()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.flush_seconds)
            try:
                self.flush()
            except Exception:
                log.exception("틱 저장 실패")

    # ───────────── 읽기 ─────────────
    def read(self, symbol: str, day: date, limit: int | None = None) -> list[dict]:
        market, _, code = symbol.partition(":")
        d = self._dir(market, day.strftime("%Y%m%d"), code)
        files = sorted(d.glob("*.parquet")) if d.is_dir() else []
        rows: list[dict] = []
        for f in files:
            rows.extend(pq.read_table(f, schema=SCHEMA).to_pylist())
        # 아직 파일로 안 쓴 버퍼도 포함
        rows.extend({"ts": t.ts, "price": _q(t.price), "qty": t.qty, "cum_volume": t.cum_volume, "bid": _q(t.bid),
                     "ask": _q(t.ask), "side": t.side, "venue": t.venue}
                    for t in self._buf.get((market, day.strftime("%Y%m%d"), code), []))
        rows.sort(key=lambda r: r["ts"])
        return rows[-limit:] if limit else rows

    def day_counts(self, day: date) -> dict[str, int]:
        """이 프로세스가 받은 그날 틱 수 (재시작 전 수신분은 빠진다)."""
        key = day.strftime("%Y%m%d")
        return {sym: n for (d, sym), n in sorted(self.counts.items()) if d == key}

    def compact_before(self, day: date) -> int:
        """day 이전 날짜 중 조각 파일이 남은 날을 모두 합친다. 합친 종목 수."""
        cutoff = day.strftime("%Y%m%d")
        days = {p.parent.parent.name for p in self.root.glob("*/*/*/part-*.parquet")}
        n = 0
        for d in sorted(x for x in days if x < cutoff):
            n += self.compact(datetime.strptime(d, "%Y%m%d").date())
        for key in [k for k in self.counts if k[0] < cutoff]:
            del self.counts[key]
        return n

    def compact(self, day: date) -> int:
        """하루치 조각 파일을 종목별 하나(ticks.parquet)로 합친다. 합친 종목 수를 돌려준다."""
        n = 0
        for market_dir in self.root.glob("*"):
            day_dir = market_dir / day.strftime("%Y%m%d")
            for code_dir in day_dir.glob("*") if day_dir.is_dir() else []:
                parts = sorted(code_dir.glob("part-*.parquet"))
                if not parts:
                    continue
                existing = code_dir / "ticks.parquet"
                tables = ([pq.read_table(existing, schema=SCHEMA)] if existing.exists() else [])
                tables += [pq.read_table(p, schema=SCHEMA) for p in parts]
                merged = pa.concat_tables(tables).sort_by("ts")
                tmp = code_dir / "ticks.parquet.tmp"
                pq.write_table(merged, tmp)
                tmp.replace(existing)
                for p in parts:
                    p.unlink()
                n += 1
        return n
