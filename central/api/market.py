"""데이터 API (/market) — docs/PLAN.md 8.4

M4 범위: 현재가, 장 상태, 환율, 종목 검색. 호가/캔들/틱/영업일은 M5 데이터 서비스에서 제공한다.
인증: 서브계좌 API 키 또는 관리자 키.
"""

from __future__ import annotations

import hmac
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Request

from central.api.common import ApiError, App, ok
from central.broker.base import Broker, BrokerUnavailable
from central.oms.errors import OmsError


def require_any(request: Request, x_api_key: Annotated[str | None, Header()] = None,
                x_admin_key: Annotated[str | None, Header()] = None) -> None:
    app = request.app.state.central
    admin = app.settings.admin_key
    if x_admin_key and admin and hmac.compare_digest(x_admin_key.encode(), admin.get_secret_value().encode()):
        return
    if app.apikeys.authenticate(x_api_key) is None:
        raise ApiError("UNAUTHORIZED", "API 키가 없거나 올바르지 않음")


router = APIRouter(prefix="/market", tags=["market"], dependencies=[Depends(require_any)])


def data_broker(app, market: str) -> Broker:
    """시세 조회에 쓸 실계좌: 해당 시장을 지원하는 실전 계좌 우선."""
    candidates = [a for a, p in app.profiles.items() if market in p.markets]
    if not candidates:
        raise OmsError("MARKET_NOT_SUPPORTED", f"{market} 을 지원하는 실계좌 없음")
    candidates.sort(key=lambda a: app.profiles[a].env != "real")
    return app.brokers[candidates[0]]


@router.get("/symbols")
def symbols(app: App, q: str = "", market: Literal["KR", "US"] | None = None, limit: int = 50):
    return ok(app.catalog.search(q, market, max(1, min(limit, 500))))


@router.get("/quote/{symbol}")
async def quote(app: App, symbol: str):
    inst = app.catalog.resolve(symbol)
    try:
        q = await data_broker(app, inst.market).quote(inst)
    except BrokerUnavailable as e:
        raise OmsError("BROKER_UNAVAILABLE", str(e)) from None
    return ok({"symbol": inst.id, "currency": inst.currency, "last": q.last,
               "upper_limit": q.upper_limit, "lower_limit": q.lower_limit, "ts": app.clock()})


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
        rate = await data_broker(app, "US").fx_rate(pair)
    except BrokerUnavailable as e:
        raise OmsError("BROKER_UNAVAILABLE", str(e)) from None
    return ok({"pair": pair, "rate": rate, "ts": app.clock()})


def _m5(name: str):
    async def handler(symbol: str = ""):
        raise OmsError("NOT_IMPLEMENTED", f"{name} 는 M5 데이터 서비스에서 제공 예정")
    return handler


router.add_api_route("/orderbook/{symbol}", _m5("호가"), methods=["GET"])
router.add_api_route("/candles/{symbol}", _m5("캔들"), methods=["GET"])
router.add_api_route("/ticks/{symbol}", _m5("저장 틱"), methods=["GET"])
router.add_api_route("/calendar", _m5("영업일"), methods=["GET"])
