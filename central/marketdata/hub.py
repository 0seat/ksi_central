"""시세 구독 허브 (docs/PLAN.md 7.1).

- 채널: "tick:KR:005930", "orderbook:US:AAPL", "candle.1m:KR:005930" (체결로 만드는 실시간 1분봉)
- 같은 채널을 여러 클라이언트가 구독해도 KIS 등록은 한 건 (채널 참조 + KIS 등록 참조, tick 과 candle.1m 은 등록 공유)
- 시장마다 시세를 받을 계좌를 하나 정한다 (settings.market_data). 그 계좌의 공유 실시간 세션에 등록한다
- 국내 체결/호가 TR: 계좌가 SOR(통합 경로)을 지원하면 통합(H0UNCNT0/H0UNASP0), 아니면 KRX
- 등록 한도를 넘으면 SUBSCRIPTION_LIMIT
- 마지막 체결가와 호가를 캐시해 현재가와 평가금액에 쓴다
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Callable, Protocol

from central.broker.kis import tr
from central.broker.kis.session import SubscriptionLimit
from central.broker.kis.ws import KisWsError, RealtimeMessage
from central.core.instruments import Instrument
from central.core.market_hours import now_kst
from central.marketdata import realtime as rt
from central.oms.catalog import InstrumentCatalog
from central.oms.errors import OmsError
from central.settings import AccountProfile, MarketDataConfig

log = logging.getLogger(__name__)

KINDS = ("tick", "orderbook", "candle.1m")
Listener = Callable[[str, object], None]   # (channel, Tick | Book)


class RealtimeSource(Protocol):
    """KisWsSession 의 시세용 인터페이스 (테스트에서는 가짜 구현)."""
    def set_handler(self, tr_id: str, handler) -> None: ...
    async def add(self, tr_id: str, tr_key: str, kind: str = "data") -> None: ...
    async def remove(self, tr_id: str, tr_key: str) -> None: ...
    on_dropped: Callable[[str, str, str], None] | None


@dataclass(frozen=True)
class LiveBar:
    symbol: str
    ts: datetime          # 봉 시작 시각
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    closed: bool


@dataclass(frozen=True)
class Feed:
    alias: str
    tr_id: str
    tr_key: str


def parse_channel(channel: str) -> tuple[str, str]:
    kind, _, symbol = channel.partition(":")
    if kind not in KINDS or ":" not in symbol:
        raise OmsError("INVALID_REQUEST", f"채널 형식은 tick:KR:005930 / orderbook:US:AAPL / candle.1m:KR:005930: {channel}")
    return kind, symbol.upper()


class MarketDataHub:
    def __init__(self, sources: dict[str, RealtimeSource], profiles: dict[str, AccountProfile],
                 catalog: InstrumentCatalog, config: MarketDataConfig,
                 clock: Callable[[], datetime] = now_kst) -> None:
        self.sources, self.profiles, self.catalog, self.clock = sources, profiles, catalog, clock
        self.config = config
        self._refs: dict[str, int] = {}
        self._feeds: dict[str, Feed] = {}
        self._feed_refs: dict[Feed, int] = {}
        self.bars: dict[str, LiveBar] = {}   # 종목별 진행 중인 1분봉
        self._lock = asyncio.Lock()
        self.listeners: list[Listener] = []
        self.last_tick: dict[str, rt.Tick] = {}
        self.last_book: dict[str, rt.Book] = {}
        for alias, src in sources.items():
            if src.on_dropped is None:
                src.on_dropped = lambda tr_id, key, reason, alias=alias: self._dropped(alias, tr_id, key, reason)

    # ───────────── 수신 계좌와 TR 결정 ─────────────
    def source_account(self, market: str) -> str:
        chosen = self.config.kr_account if market == "KR" else self.config.us_account
        if chosen is None:
            candidates = sorted((a for a, p in self.profiles.items() if market in p.markets),
                                key=lambda a: self.profiles[a].env != "real")
            chosen = next((a for a in candidates if a in self.sources), None)
        if chosen is None or chosen not in self.sources:
            raise OmsError("MARKET_NOT_SUPPORTED", f"{market} 실시간 시세를 받을 계좌 없음")
        return chosen

    def feed(self, kind: str, inst: Instrument) -> Feed:
        alias = self.source_account(inst.market)
        if inst.market == "KR":
            unified = "SOR" in self.profiles[alias].kr_routes
            if kind != "orderbook":
                tr_id = tr.WS_KR_TICK_UNIFIED if unified else tr.WS_KR_TICK_KRX
            else:
                tr_id = tr.WS_KR_ORDERBOOK_UNIFIED if unified else tr.WS_KR_ORDERBOOK_KRX
            return Feed(alias, tr_id, inst.code)
        tr_id = tr.WS_US_ORDERBOOK if kind == "orderbook" else tr.WS_US_TICK
        return Feed(alias, tr_id, tr.us_ws_key(inst.exchange, inst.code))

    # ───────────── 구독 ─────────────
    async def acquire(self, channel: str) -> str:
        """채널 참조를 하나 늘린다. 처음이면 KIS 에 등록. 정규화된 채널 이름을 돌려준다."""
        kind, symbol = parse_channel(channel)
        inst = self.catalog.resolve(symbol)
        channel = f"{kind}:{inst.id}"
        async with self._lock:
            if self._refs.get(channel, 0) == 0:
                feed = self.feed(kind, inst)
                if self._feed_refs.get(feed, 0) == 0:
                    src = self.sources[feed.alias]
                    src.set_handler(feed.tr_id, self._handler(feed.tr_id))
                    try:
                        await src.add(feed.tr_id, feed.tr_key, "data")
                    except SubscriptionLimit as e:
                        raise OmsError("SUBSCRIPTION_LIMIT", str(e)) from None
                    except KisWsError as e:
                        raise OmsError("BROKER_UNAVAILABLE", str(e)) from None
                    log.info("시세 등록 %s:%s (%s)", feed.tr_id, feed.tr_key, feed.alias)
                self._feed_refs[feed] = self._feed_refs.get(feed, 0) + 1
                self._feeds[channel] = feed
            self._refs[channel] = self._refs.get(channel, 0) + 1
        return channel

    async def release(self, channel: str) -> None:
        async with self._lock:
            n = self._refs.get(channel, 0)
            if n <= 0:
                return
            if n > 1:
                self._refs[channel] = n - 1
                return
            self._refs.pop(channel, None)
            feed = self._feeds.pop(channel, None)
            if feed is None:
                return
            left = self._feed_refs.get(feed, 1) - 1
            if left > 0:
                self._feed_refs[feed] = left
                return
            self._feed_refs.pop(feed, None)
            await self.sources[feed.alias].remove(feed.tr_id, feed.tr_key)
            log.info("시세 해제 %s:%s (%s)", feed.tr_id, feed.tr_key, feed.alias)

    async def start_watchlist(self) -> None:
        for symbol in self.config.watchlist:
            try:
                await self.acquire(f"tick:{symbol}")
            except OmsError as e:
                log.error("상시 수집 등록 실패 %s: %s %s", symbol, e.code, e.message)

    def stats(self) -> dict:
        return {"channels": dict(sorted(self._refs.items())),
                "sources": {m: self._safe_source(m) for m in ("KR", "US")}}

    def _safe_source(self, market: str) -> str | None:
        try:
            return self.source_account(market)
        except OmsError:
            return None

    # ───────────── 캐시 ─────────────
    def last_price(self, symbol: str) -> Decimal | None:
        t = self.last_tick.get(symbol)
        return t.price if t else None

    # ───────────── 수신 ─────────────
    def _handler(self, tr_id: str):
        async def handle(msg: RealtimeMessage) -> None:
            today = self.clock().date()
            for rec in msg.records:
                item = self._parse(tr_id, rec, today)
                if item is None:
                    continue
                if isinstance(item, rt.Tick):
                    self.last_tick[item.symbol] = item
                    self._publish(f"tick:{item.symbol}", item)
                    candle_ch = f"candle.1m:{item.symbol}"
                    for bar in self._update_bar(item):
                        if self._refs.get(candle_ch):
                            self._publish(candle_ch, bar)
                else:
                    self.last_book[item.symbol] = item
                    self._publish(f"orderbook:{item.symbol}", item)
        return handle

    def _publish(self, channel: str, item: object) -> None:
        for listener in self.listeners:
            try:
                listener(channel, item)
            except Exception:
                log.exception("시세 리스너 오류 %s", channel)

    def _update_bar(self, t: rt.Tick) -> list[LiveBar]:
        """체결로 1분봉 갱신. 분이 바뀌면 직전 봉을 closed=True 로 먼저 내보낸다. 봉 시각은 시작 시각."""
        start = t.ts.replace(second=0, microsecond=0)
        cur = self.bars.get(t.symbol)
        out: list[LiveBar] = []
        if cur is not None and start < cur.ts:
            return out  # 늦게 온 체결은 무시
        if cur is not None and start > cur.ts:
            out.append(replace(cur, closed=True))
            cur = None
        if cur is None:
            cur = LiveBar(t.symbol, start, t.price, t.price, t.price, t.price, t.qty, False)
        else:
            cur = replace(cur, high=max(cur.high, t.price), low=min(cur.low, t.price), close=t.price,
                          volume=cur.volume + t.qty)
        self.bars[t.symbol] = cur
        out.append(cur)
        return out

    @staticmethod
    def _parse(tr_id: str, rec: list[str], today):
        if tr_id in rt.KR_TICK_TRS:
            return rt.parse_kr_tick(tr_id, rec, today)
        if tr_id in rt.KR_BOOK_TRS:
            return rt.parse_kr_book(tr_id, rec, today)
        if tr_id == rt.US_TICK_TR:
            return rt.parse_us_tick(rec, today)
        if tr_id == rt.US_BOOK_TR:
            return rt.parse_us_book(rec, today)
        return None

    def _dropped(self, alias: str, tr_id: str, tr_key: str, reason: str) -> None:
        """재연결 후 KIS 가 등록을 거부한 시세: 채널 상태를 지우고 알린다."""
        dropped = Feed(alias, tr_id, tr_key)
        self._feed_refs.pop(dropped, None)
        for channel, feed in list(self._feeds.items()):
            if feed == dropped:
                self._feeds.pop(channel, None)
                self._refs.pop(channel, None)
                log.error("시세 채널 중단 %s: %s", channel, reason)
                self._publish(f"dropped:{channel}", reason)
