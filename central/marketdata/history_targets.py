"""과거 시세 보관 대상 (central/marketdata/history.py 가 매일 받는 종목과 지수).

대상은 세 군데에서 모인다.
  config      central.toml [history] symbols / indices (운영자가 파일로 고정)
  nasdaq100   [history] nasdaq100 = true 이면 나스닥100 구성종목과 INDEX:US:NDX (기본 켬).
              KIS 미국 1분봉은 약 1개월만 남으므로 지나가면 다시 받을 수 없다.
  sub:<id>    앱이 API 로 등록 (POST /market/history-subscriptions). admin 은 관리자 키로 등록한 것.

구성종목은 nasdaq.com 공개 목록을 매일 받아 data/history/nasdaq100.json 에 두고,
받은 적이 없거나 실패하면 저장소에 넣어 둔 nasdaq100.txt 를 쓴다. 구성에서 빠진 종목은 수집만 멈추고 보관본은 남긴다.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import httpx
from sqlalchemy import delete, select

from central.core.market_hours import now_kst
from central.marketdata.history import SeriesKey
from central.storage.db import Database
from central.storage.models import HistorySubscriptionRow

log = logging.getLogger(__name__)

NASDAQ100 = "nasdaq100"
NASDAQ100_INDEX = "INDEX:US:NDX"
NASDAQ100_URL = "https://api.nasdaq.com/api/quote/list-type/nasdaq100"
_BUNDLED = Path(__file__).with_name("nasdaq100.txt")
_TICKER = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_INDEX_CODE = re.compile(r"^[A-Z0-9.]{1,12}$")


def _clean(symbols: list[str]) -> list[str]:
    return sorted({s.strip().upper() for s in symbols if _TICKER.match(s.strip().upper())})


def parse_nasdaq_list(body: dict) -> list[str]:
    """nasdaq.com 응답에서 티커만. 형식이 바뀌었거나 너무 적으면 ValueError (잘못된 목록으로 덮어쓰지 않게)."""
    rows = ((body.get("data") or {}).get("data") or {}).get("rows") or []
    out = _clean([r.get("symbol", "") for r in rows])
    if not 90 <= len(out) <= 110:
        raise ValueError(f"나스닥100 구성종목 수가 이상함: {len(out)}")
    return out


class HistoryTargets:
    def __init__(self, db: Database, root: Path, settings_history, *,
                 clock: Callable[[], datetime] = now_kst) -> None:
        self.db, self.root, self.cfg, self.clock = db, root, settings_history, clock

    # ───────────── 나스닥100 구성종목 ─────────────
    @property
    def _cache(self) -> Path:
        return self.root / "nasdaq100.json"

    def nasdaq100(self) -> tuple[list[str], str]:
        """(티커 목록, 출처 설명)."""
        try:
            d = json.loads(self._cache.read_text(encoding="utf-8"))
            return _clean(d["symbols"]), f"nasdaq.com {d['as_of']}"
        except (OSError, ValueError, KeyError):
            lines = _BUNDLED.read_text(encoding="utf-8").splitlines()
            return _clean([x for x in lines if x and not x.startswith("#")]), "저장소 기본 목록"

    async def refresh_nasdaq100(self, http: httpx.AsyncClient) -> dict:
        """구성종목을 다시 받아 캐시에 쓴다. 바뀐 종목을 돌려준다."""
        before, _ = self.nasdaq100()
        r = await http.get(NASDAQ100_URL, timeout=20,
                           headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        r.raise_for_status()
        symbols = parse_nasdaq_list(r.json())
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self._cache.with_suffix(".tmp")
        tmp.write_text(json.dumps({"as_of": self.clock().date().isoformat(), "symbols": symbols}), encoding="utf-8")
        tmp.replace(self._cache)
        return {"count": len(symbols), "added": sorted(set(symbols) - set(before)),
                "removed": sorted(set(before) - set(symbols))}

    # ───────────── 앱 등록 ─────────────
    def subscribe(self, key: str, owner: str) -> bool:
        """등록. 이미 같은 owner 가 등록했으면 False."""
        k = str(SeriesKey.parse(key))
        if k.startswith("INDEX:") and not _INDEX_CODE.match(SeriesKey.parse(k).code):
            raise ValueError(f"지수 코드 형식 오류: {key}")
        with self.db.session() as s, s.begin():
            if s.get(HistorySubscriptionRow, (k, owner)) is not None:
                return False
            s.add(HistorySubscriptionRow(symbol=k, owner=owner, created_at=self.clock()))
            return True

    def unsubscribe(self, key: str, owner: str | None) -> int:
        """owner 의 등록을 지운다. owner=None 이면 그 종목의 앱 등록 전부 (관리자). 지운 개수."""
        k = str(SeriesKey.parse(key))
        with self.db.session() as s, s.begin():
            q = delete(HistorySubscriptionRow).where(HistorySubscriptionRow.symbol == k)
            if owner is not None:
                q = q.where(HistorySubscriptionRow.owner == owner)
            return s.execute(q).rowcount or 0

    # ───────────── 합친 목록 ─────────────
    def sources(self) -> dict[str, list[str]]:
        """{보관 키: [출처...]}. 출처는 config, nasdaq100, sub:<id>, admin."""
        out: dict[str, list[str]] = {}

        def add(k: str, src: str) -> None:
            out.setdefault(k, [])
            if src not in out[k]:
                out[k].append(src)

        for k in self.cfg.symbols + self.cfg.indices:
            add(k, "config")
        if self.cfg.nasdaq100:
            for t in self.nasdaq100()[0]:
                add(f"US:{t}", NASDAQ100)
            add(NASDAQ100_INDEX, NASDAQ100)
        with self.db.session() as s:
            for row in s.scalars(select(HistorySubscriptionRow).order_by(HistorySubscriptionRow.created_at)):
                add(row.symbol, row.owner)
        return dict(sorted(out.items()))

    def symbols(self, market: str | None = None) -> list[str]:
        return [k for k in self.sources() if not k.startswith("INDEX:") and (market is None or k.startswith(f"{market}:"))]

    def indices(self, market: str | None = None) -> list[str]:
        return [k for k in self.sources() if k.startswith("INDEX:") and (market is None or k.startswith(f"INDEX:{market}:"))]
