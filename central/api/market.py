"""데이터 API (/market) — docs/PLAN.md 8.4

현재가와 호가는 실시간 수신 중이면 실시간 값, 아니면 REST. 캔들은 캐시 우선(central/marketdata/candles.py).
종목코드에 '/' 가 들어갈 수 있어(예: US:BRK/B) 경로 파라미터는 path 형식이다.
인증: 서브계좌 API 키 또는 관리자 키.
"""

from __future__ import annotations

import datetime as dt
import hmac
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Request

from central.api.common import ApiError, App, ok
from central.broker.base import BrokerUnavailable
from central.marketdata.candles import INTERVALS, MINUTE_INTERVALS
from central.oms.errors import OmsError


def require_any(request: Request, x_api_key: Annotated[str | None, Header()] = None,
                x_admin_key: Annotated[str | None, Header()] = None) -> None:
    app = request.app.state.central
    admin = app.settings.admin_key
    if x_admin_key and admin and hmac.compare_digest(x_admin_key.encode(), admin.get_secret_value().encode()):
        return
    if app.apikeys.authenticate(x_api_key) is None:
        raise ApiError("UNAUTHORIZED", "API 키가 없거나 올바르지 않음")


REALTIME_FRESH_SEC = 10
# from 을 생략했을 때 조회 기간
_DEFAULT_SPAN = {"1d": dt.timedelta(days=200), "1w": dt.timedelta(days=3 * 365), "1M": dt.timedelta(days=10 * 365)}

router = APIRouter(prefix="/market", tags=["market"], dependencies=[Depends(require_any)])


def _fresh(app, ts: dt.datetime) -> bool:
    return (app.clock() - ts).total_seconds() <= REALTIME_FRESH_SEC


@router.get("/symbols")
def symbols(app: App, q: str = "", market: Literal["KR", "US"] | None = None, limit: int = 50):
    return ok(app.catalog.search(q, market, max(1, min(limit, 500))))


@router.get("/quote/{symbol:path}")
async def quote(app: App, symbol: str, source: Literal["auto", "realtime", "rest"] = "auto"):
    """auto: 실시간 수신 중이고 최근 체결이 REALTIME_FRESH_SEC 이내면 실시간 값, 아니면 REST 조회."""
    """상한가/하한가는 두 경우 모두 들어간다 (실시간일 때는 그날 처음 한 번 REST 로 받아 캐시)."""
    inst = app.catalog.resolve(symbol)
    tick = app.market.last_tick.get(inst.id)
    if source == "realtime" or (source == "auto" and tick is not None and _fresh(app, tick.ts)):
        if tick is None:
            raise OmsError("NOT_FOUND", f"{inst.id} 실시간 체결 수신 기록 없음 (tick 채널 구독 필요)")
        upper, lower = await _limits(app, inst)
        return ok({"symbol": inst.id, "currency": inst.currency, "last": tick.price, "bid": tick.bid,
                   "ask": tick.ask, "cum_volume": tick.cum_volume, "venue": tick.venue, "upper_limit": upper,
                   "lower_limit": lower, "ts": tick.ts, "source": "realtime"})
    q = await _rest_quote(app, inst)
    return ok({"symbol": inst.id, "currency": inst.currency, "last": q.last,
               "upper_limit": q.upper_limit, "lower_limit": q.lower_limit, "ts": app.clock(), "source": "rest"})


async def _rest_quote(app, inst):
    try:
        q = await app.data_broker(inst.market).quote(inst)
    except BrokerUnavailable as e:
        raise OmsError("BROKER_UNAVAILABLE", str(e)) from None
    app.price_limits[(inst.id, app.clock().date())] = (q.upper_limit, q.lower_limit)
    return q


async def _limits(app, inst) -> tuple:
    """가격제한폭은 하루 동안 바뀌지 않는다 (미국은 없음)."""
    if inst.market != "KR":
        return None, None
    key = (inst.id, app.clock().date())
    if key not in app.price_limits:
        if len(app.price_limits) > 5000:
            app.price_limits.clear()
        await _rest_quote(app, inst)
    return app.price_limits[key]


@router.get("/orderbook/{symbol:path}")
async def orderbook(app: App, symbol: str, source: Literal["auto", "realtime", "rest"] = "auto"):
    """10단계 호가. 미국은 무료 시세가 1호가뿐이라 1단계만 채워진다."""
    inst = app.catalog.resolve(symbol)
    book = app.market.last_book.get(inst.id)
    if source == "realtime" or (source == "auto" and book is not None and _fresh(app, book.ts)):
        if book is None:
            raise OmsError("NOT_FOUND", f"{inst.id} 실시간 호가 수신 기록 없음 (orderbook 채널 구독 필요)")
        return ok({"symbol": inst.id, "ts": book.ts, "asks": book.asks, "bids": book.bids, "source": "realtime"})
    try:
        b = await app.data_broker(inst.market).orderbook(inst)
    except BrokerUnavailable as e:
        raise OmsError("BROKER_UNAVAILABLE", str(e)) from None
    return ok({"symbol": inst.id, "ts": b.ts, "asks": b.asks, "bids": b.bids, "source": "rest"})


@router.get("/candles/{symbol:path}")
async def candles(app: App, symbol: str, interval: str = "1d",
                  start: dt.datetime | dt.date | None = Query(default=None, alias="from"),
                  end: dt.datetime | dt.date | None = Query(default=None, alias="to"),
                  limit: int = Query(default=500, ge=1, le=5000)):
    """봉 시각은 시작 시각(KST). 일/주/월봉은 거래일 0시(KST 표기). 분봉은 정규장만.

    from 생략 시: 분봉은 to 기준 하루 전부터, 일봉 200일, 주봉 3년, 월봉 10년. 결과는 최근 limit 개.
    """
    if interval not in INTERVALS:
        raise OmsError("INVALID_REQUEST", f"interval 은 {INTERVALS} 중 하나")
    inst = app.catalog.resolve(symbol)
    now = app.clock()
    end_dt = _as_dt(end, end_of_day=True) if end else now
    if start:
        start_dt = _as_dt(start)
    else:
        start_dt = end_dt - (dt.timedelta(days=1) if interval in MINUTE_INTERVALS else _DEFAULT_SPAN[interval])
    try:
        result = await app.candles.get(inst, interval, start_dt, end_dt)
    except BrokerUnavailable as e:
        raise OmsError("BROKER_UNAVAILABLE", str(e)) from None
    bars = result.candles[-limit:]
    return ok({"symbol": inst.id, "interval": interval, "currency": inst.currency, "truncated": result.truncated,
               "candles": [{"ts": b.ts, "open": b.open, "high": b.high, "low": b.low, "close": b.close,
                            "volume": b.volume} for b in bars]})


@router.get("/history/{symbol:path}")
def history(app: App, symbol: str, interval: Literal["1m", "1d"] = "1d",
            adjust: Literal["none", "split", "total"] = "none",
            start: dt.datetime | dt.date | None = Query(default=None, alias="from"),
            end: dt.datetime | dt.date | None = Query(default=None, alias="to"),
            limit: int = Query(default=5000, ge=1, le=100000)):
    """보관소(원주가 + 기업행위)에서 조회. KIS 에서 만료된 과거 분봉도 여기 있으면 나온다 (central/marketdata/history.py).

    symbol: KR:005930, US:AAPL, INDEX:KR:0001(코스피), INDEX:US:COMP(나스닥 종합). 분봉은 정규장만.
    adjust: none 원주가, split 분할/병합 등 자본변동만, total 배당까지 (국내는 split 과 같음). 지수는 수정 없음.
    결과는 기간 안의 최근 limit 개. 일봉 ts 는 거래일 0시(KST 표기), events 는 이 종목의 기업행위 목록.
    """
    from central.marketdata.history import HistoryStore, SeriesKey
    from central.marketdata.history_cli import history_root

    try:
        key = SeriesKey.parse(symbol)
    except ValueError as e:
        raise OmsError("INVALID_REQUEST", str(e)) from None
    store = HistoryStore(history_root(app.settings))

    def local(v, end_of_day=False):   # 날짜만 주면 그 시장의 현지 날짜로
        if v is None or isinstance(v, dt.datetime):
            return _as_dt(v, end_of_day) if v else None
        return dt.datetime.combine(v, dt.time.max if end_of_day else dt.time.min, key.zone)

    bars = store.read(key, interval, local(start), local(end, True), adjust)[-limit:]
    return ok({"symbol": str(key), "interval": interval, "adjust": adjust,
               "events": [e.to_json() for e in store.events(key)] if not key.index else [],
               "candles": [{"ts": b.ts, "open": b.open, "high": b.high, "low": b.low, "close": b.close,
                            "volume": b.volume} for b in bars]})


@router.get("/status")
def status(app: App, market: Literal["KR", "US"] | None = None):
    now = app.clock()
    result = {}
    for m in [market] if market else ["KR", "US"]:
        open_, reason = app.hours.can_place_order(m, now)
        result[m] = {"accepting_orders": open_, "reason": reason or None}
    return ok({"ts": now, "markets": result})


@router.get("/fx")
async def fx(app: App, pair: Literal["USDKRW"] = "USDKRW"):
    try:
        rate = await app.data_broker("US").fx_rate(pair)
    except BrokerUnavailable as e:
        raise OmsError("BROKER_UNAVAILABLE", str(e)) from None
    return ok({"pair": pair, "rate": rate, "ts": app.clock()})


@router.get("/ticks/{symbol:path}")
def ticks(app: App, symbol: str, date: dt.date | None = None, limit: int = Query(default=10000, ge=1, le=200000)):
    """저장된 틱 (기본: 오늘). 상시 수집(watchlist)이나 누군가 tick 채널을 구독하는 동안의 데이터만 있다."""
    if app.ticks is None:
        raise OmsError("NOT_IMPLEMENTED", "틱 저장이 꺼져 있음 (market_data.store_ticks)")
    inst = app.catalog.resolve(symbol)
    return ok({"symbol": inst.id, "rows": app.ticks.read(inst.id, date or app.clock().date(), limit)})


@router.get("/calendar")
async def calendar(app: App, market: Literal["KR", "US"] = "KR",
                   start: dt.date | None = Query(default=None, alias="from"),
                   end: dt.date | None = Query(default=None, alias="to")):
    """개장일. source: kis(KIS 휴장일 조회), weekday(아직 받지 않은 국내 날짜, 평일=개장 추정),
    rule(미국 NYSE 규칙), manual(수동 반영). 미국 조기폐장일은 early_close_et."""
    today = app.clock().date()
    start = start or today
    end = end or start + dt.timedelta(days=30)
    if (end - start).days > 400:
        raise OmsError("INVALID_REQUEST", "조회 기간은 400일 이내")
    return ok({"market": market, "days": app.calendar.days(market, start, end)})


def _as_dt(v: dt.datetime | dt.date, end_of_day: bool = False) -> dt.datetime:
    from central.core.market_hours import KST

    if isinstance(v, dt.datetime):
        return v if v.tzinfo else v.replace(tzinfo=KST)
    return dt.datetime.combine(v, dt.time.max if end_of_day else dt.time.min, KST)
