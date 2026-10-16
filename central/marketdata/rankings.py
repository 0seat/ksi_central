"""순위 조회 (/market/rankings): 거래량, 등락률, 체결강도, 미국 거래량 급증과 가격 급등락.

브로커가 KIS 응답을 공통 행으로 바꾸고(central/broker/kis/ranking.py), 여기서는 조건 검사와 짧은 캐시를 맡는다.
여러 앱이 같은 조건으로 부르면 TTL 동안 KIS 를 한 번만 부른다 (동시에 들어온 요청도 한 번으로 합친다).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Callable

from central.broker.base import Broker, BrokerUnavailable
from central.oms.errors import OmsError

TTL_SEC = 10
MAX_CACHE = 200

KINDS: dict[str, str] = {
    "kr_volume": "KR",          # 거래량 순위 (sort: 거래량, 거래증가율, 회전율, 거래대금)
    "kr_fluctuation": "KR",     # 등락률 순위 (전일 종가 기준)
    "kr_volume_power": "KR",    # 체결강도 순위
    "us_volume_surge": "US",    # N분 전 대비 거래량 급증
    "us_price_fluct": "US",     # N분 전 대비 가격 급등/급락
}
SORTS: dict[str, tuple[str, ...]] = {   # 첫 값이 기본
    "kr_volume": ("volume", "volume_growth", "volume_turnover", "value", "value_turnover"),
    "kr_fluctuation": ("up", "down", "open_up", "open_down", "range"),
    "us_price_fluct": ("up", "down"),
}
UNIVERSES = ("all", "kospi", "kosdaq", "kospi200")
STOCK_TYPES = ("all", "common", "preferred")
# KIS 대상 제외 구분 코드 10자리의 순서
EXCLUDES = ("caution", "managed", "liquidation", "unfaithful", "preferred", "halted", "etf", "etn",
            "no_credit", "spac")
US_EXCHANGES = ("NAS", "NYS", "AMS")
US_MINUTES = (1, 2, 3, 5, 10, 15, 20, 30, 60, 120)
US_VOLUME_STEPS = (0, 100, 1_000, 10_000, 100_000, 1_000_000, 10_000_000)


@dataclass(frozen=True)
class RankingQuery:
    kind: str
    sort: str | None = None
    universe: str = "all"
    stock_type: str = "all"
    exclude: tuple[str, ...] = ()
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    min_volume: int | None = None
    min_rate: Decimal | None = None
    max_rate: Decimal | None = None
    exchange: str = "NAS"
    minutes: int = 5

    @property
    def market(self) -> str:
        return KINDS[self.kind]

    @property
    def us_volume_step(self) -> int:
        """KIS 미국 순위의 거래량 조건은 단계값만 받는다: min_volume 이하의 가장 큰 단계 (나머지는 행 필터)."""
        return max(i for i, v in enumerate(US_VOLUME_STEPS) if v <= (self.min_volume or 0))

    def to_json(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if v not in (None, ())}
        if self.market == "KR":
            for k in ("exchange", "minutes"):
                d.pop(k, None)
        else:
            for k in ("universe", "stock_type"):
                d.pop(k, None)
        return d


def build_query(kind: str, *, sort: str | None = None, universe: str = "all", stock_type: str = "all",
                exclude: str | None = None, min_price: Decimal | None = None, max_price: Decimal | None = None,
                min_volume: int | None = None, min_rate: Decimal | None = None, max_rate: Decimal | None = None,
                exchange: str = "NAS", minutes: int = 5) -> RankingQuery:
    """API 쿼리 문자열을 검사해 RankingQuery 로. 잘못된 값은 INVALID_REQUEST."""
    def bad(msg: str) -> OmsError:
        return OmsError("INVALID_REQUEST", msg)

    if kind not in KINDS:
        raise bad(f"kind 는 {', '.join(KINDS)} 중 하나")
    market = KINDS[kind]
    if kind in SORTS:
        sort = sort or SORTS[kind][0]
        if sort not in SORTS[kind]:
            raise bad(f"{kind} 의 sort 는 {', '.join(SORTS[kind])} 중 하나")
    elif sort:
        raise bad(f"{kind} 는 sort 를 받지 않음")
    excl = tuple(x.strip() for x in (exclude or "").split(",") if x.strip())
    if any(x not in EXCLUDES for x in excl):
        raise bad(f"exclude 는 쉼표로 구분한 {', '.join(EXCLUDES)}")
    if min_volume is not None and min_volume < 0:
        raise bad("min_volume 은 0 이상")
    if market == "KR":
        if universe not in UNIVERSES:
            raise bad(f"universe 는 {', '.join(UNIVERSES)} 중 하나")
        if stock_type not in STOCK_TYPES:
            raise bad(f"stock_type 은 {', '.join(STOCK_TYPES)} 중 하나")
        if kind == "kr_fluctuation" and stock_type != "all":
            raise bad("kr_fluctuation 은 stock_type 을 받지 않음 (exclude=preferred 를 쓸 것)")
        if (min_rate is not None or max_rate is not None) and kind != "kr_fluctuation":
            raise bad("min_rate, max_rate 는 kr_fluctuation 만")
    else:
        exchange = exchange.upper()
        if exchange not in US_EXCHANGES:
            raise bad(f"exchange 는 {', '.join(US_EXCHANGES)} 중 하나")
        if minutes not in US_MINUTES:
            raise bad(f"minutes 는 {US_MINUTES} 중 하나")
        if excl or min_price is not None or max_price is not None or min_rate is not None or max_rate is not None:
            raise bad("미국 순위는 exclude, min_price, max_price, min_rate, max_rate 를 받지 않음")
    return RankingQuery(kind, sort, universe, stock_type, excl, min_price, max_price, min_volume,
                        min_rate, max_rate, exchange, minutes)


@dataclass
class _Entry:
    at: float
    ts: datetime
    rows: list[dict] = field(default_factory=list)


class RankingService:
    def __init__(self, broker_for: Callable[[str], Broker], clock: Callable[[], datetime],
                 ttl: float = TTL_SEC) -> None:
        self.broker_for = broker_for
        self.clock = clock
        self.ttl = ttl
        self._cache: dict[RankingQuery, _Entry] = {}
        self._inflight: dict[RankingQuery, asyncio.Future] = {}

    async def get(self, q: RankingQuery) -> tuple[list[dict], datetime, bool]:
        """(행, 조회 시각, 캐시 여부)."""
        hit = self._cache.get(q)
        if hit is not None and time.monotonic() - hit.at < self.ttl:
            return hit.rows, hit.ts, True
        if q in self._inflight:
            entry = await asyncio.shield(self._inflight[q])
            return entry.rows, entry.ts, True
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[q] = fut
        try:
            rows = await self.broker_for(q.market).ranking(q)
            if q.min_volume:
                rows = [r for r in rows if r.get("volume", 0) >= q.min_volume]
            entry = _Entry(time.monotonic(), self.clock(), rows)
            if len(self._cache) >= MAX_CACHE:
                self._cache.clear()
            self._cache[q] = entry
            fut.set_result(entry)
            return rows, entry.ts, False
        except BrokerUnavailable as e:
            err = OmsError("BROKER_UNAVAILABLE", str(e))
            fut.set_exception(err)
            fut.exception()   # 기다리는 쪽이 없어도 경고가 나지 않게
            raise err from None
        except asyncio.CancelledError:
            fut.cancel()
            raise
        except Exception as e:
            fut.set_exception(e)
            fut.exception()
            raise
        finally:
            del self._inflight[q]
