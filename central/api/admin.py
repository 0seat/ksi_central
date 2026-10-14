"""관리자 API (/admin, X-Admin-Key) — docs/PLAN.md 8.2"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from central.api.common import App, Money, ok, require_admin
from central.broker.base import BrokerUnavailable
from central.oms import queries
from central.oms.errors import OmsError
from central.oms.fees import FeeRule
from central.risk.engine import RiskLimits

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])

MarketCode = Literal["KR", "US"]
CurrencyCode = Literal["KRW", "USD"]


class _Body(BaseModel):
    model_config = {"extra": "forbid"}


# ───────────── 실계좌 ─────────────
def _broker_view(app, alias: str) -> dict:
    p = app.profiles[alias]
    return {
        "id": alias, "env": p.env, "markets": p.markets, "kr_routes": p.kr_routes,
        "kr_default_route": p.kr_default_route, "integrated_margin": p.integrated_margin,
        "trading_enabled": p.trading_enabled, "usd_inventory_target": p.usd_inventory_target,
        "usd_inventory_min": p.usd_inventory_min, "health": app.brokers[alias].health(),
    }


def _alias(app, alias: str) -> str:
    if alias not in app.profiles:
        raise OmsError("NOT_FOUND", f"실계좌 {alias} 없음")
    return alias


@router.get("/broker-accounts")
def broker_accounts(app: App):
    return ok([_broker_view(app, a) for a in app.profiles])


@router.get("/broker-accounts/{alias}/health")
def broker_health(app: App, alias: str):
    _alias(app, alias)
    halted = [h for h in app.halted() if h["scope"] == "all" or h["id"] in (alias, f"{alias}:KR", f"{alias}:US")]
    return ok({**app.brokers[alias].health(), "halted": halted})


class BrokerPatch(_Body):
    trading_enabled: bool


@router.patch("/broker-accounts/{alias}")
def broker_patch(app: App, alias: str, body: BrokerPatch):
    app.set_trading_enabled(_alias(app, alias), body.trading_enabled)
    return ok(_broker_view(app, alias))


@router.post("/broker-accounts/{alias}/bootstrap")
async def broker_bootstrap(app: App, alias: str):
    """첫 실행: 실계좌 현금과 보유종목을 미배정으로 들여온다 (이미 기록이 있으면 거부)."""
    summary = await app.bootstrap_if_empty(_alias(app, alias))
    if summary is None:
        raise OmsError("ALREADY_BOOTSTRAPPED", "미배정 서브계좌에 이미 기록이 있음")
    return ok(summary)


# ───────────── 서브계좌 ─────────────
def _sub_view(app, sub) -> dict:
    balances = app.accounts.balances(sub.id)
    return {
        "id": sub.id, "broker_account_id": sub.broker_account_id, "name": sub.name, "type": sub.type,
        "status": sub.status, "markets": sub.markets, "fx_mode": sub.fx_mode,
        "balances": {c: {"cash": b.cash, "held": b.held, "available": b.available} for c, b in balances.items()},
    }


class SubCreate(_Body):
    broker_account_id: str
    name: str = Field(min_length=1, max_length=64)
    type: Literal["strategy", "app"] = "strategy"
    markets: list[MarketCode] = Field(default_factory=lambda: ["KR", "US"])
    fx_mode: Literal["none", "broker"] = "none"


@router.post("/sub-accounts")
def sub_create(app: App, body: SubCreate):
    sub = app.accounts.create_sub_account(body.broker_account_id, body.name, body.type, body.markets, body.fx_mode)
    return ok(_sub_view(app, sub))


@router.get("/sub-accounts")
def sub_list(app: App, broker_account_id: str | None = None):
    return ok([_sub_view(app, s) for s in app.accounts.list(broker_account_id)])


@router.get("/sub-accounts/{sub_id}")
def sub_get(app: App, sub_id: int):
    sub = app.accounts.get(sub_id)
    return ok({**_sub_view(app, sub), "positions": app.accounts.positions(sub_id),
               "risk_limits": queries.risk_limits(app.db, sub_id), "api_keys": app.apikeys.list(sub_id)})


class SubPatch(_Body):
    status: Literal["active", "suspended", "closed"]


@router.patch("/sub-accounts/{sub_id}")
def sub_patch(app: App, sub_id: int, body: SubPatch):
    return ok(_sub_view(app, app.accounts.set_status(sub_id, body.status)))


class RiskLimitsBody(_Body):
    market: MarketCode
    max_order_amount: Money | None = None
    max_position_amount: Money | None = None
    max_daily_loss: Money | None = None
    max_open_orders: int | None = Field(default=None, ge=0)
    allowed_symbols: list[str] | None = None


@router.put("/sub-accounts/{sub_id}/risk-limits")
def sub_risk_limits(app: App, sub_id: int, body: RiskLimitsBody):
    app.accounts.get(sub_id)
    symbols = [s.upper() for s in body.allowed_symbols] if body.allowed_symbols is not None else None
    app.risk.set_limits(sub_id, body.market, RiskLimits(
        body.max_order_amount, body.max_position_amount, body.max_daily_loss, body.max_open_orders, symbols))
    return ok(queries.risk_limits(app.db, sub_id))


# ───────────── API 키 ─────────────
class KeyCreate(_Body):
    label: str | None = Field(default=None, max_length=64)


@router.post("/sub-accounts/{sub_id}/api-keys")
def key_issue(app: App, sub_id: int, body: KeyCreate | None = None):
    info, key = app.apikeys.issue(sub_id, body.label if body else None)
    # 평문 키는 이 응답에서 한 번만 보여 준다
    return ok({**ok(info)["data"], "api_key": key})


@router.get("/api-keys")
def key_list(app: App, sub_account_id: int | None = None):
    return ok(app.apikeys.list(sub_account_id))


@router.delete("/api-keys/{key_id}")
def key_revoke(app: App, key_id: int):
    return ok(app.apikeys.revoke(key_id))


# ───────────── 자금 / 포지션 ─────────────
class TransferBody(_Body):
    from_sub_account_id: int = Field(alias="from")
    to_sub_account_id: int = Field(alias="to")
    currency: CurrencyCode
    amount: Money
    memo: str = Field(default="", max_length=200)


@router.post("/transfers")
def transfer(app: App, body: TransferBody):
    app.accounts.transfer(body.from_sub_account_id, body.to_sub_account_id, body.currency, body.amount, body.memo)
    return ok({"from": _sub_view(app, app.accounts.get(body.from_sub_account_id)),
               "to": _sub_view(app, app.accounts.get(body.to_sub_account_id))})


class FxBody(_Body):
    sub_account_id: int
    from_currency: CurrencyCode
    from_amount: Money = Field(gt=0)
    to_currency: CurrencyCode
    to_amount: Money = Field(gt=0)


@router.post("/fx")
def fx(app: App, body: FxBody):
    """HTS 등에서 실제로 환전한 결과를 장부에 반영 (보통 미배정 서브계좌)."""
    if body.from_currency == body.to_currency:
        raise OmsError("INVALID_REQUEST", "서로 다른 통화여야 함")
    app.accounts.record_fx(body.sub_account_id, body.from_currency, body.from_amount, body.to_currency, body.to_amount)
    return ok(_sub_view(app, app.accounts.get(body.sub_account_id)))


class PositionTransferBody(_Body):
    from_sub_account_id: int = Field(alias="from")
    to_sub_account_id: int = Field(alias="to")
    symbol: str
    qty: int = Field(gt=0)


@router.post("/positions/transfer")
def position_transfer(app: App, body: PositionTransferBody):
    app.accounts.transfer_position(body.from_sub_account_id, body.to_sub_account_id, body.symbol.upper(), body.qty)
    return ok({"from": app.accounts.positions(body.from_sub_account_id),
               "to": app.accounts.positions(body.to_sub_account_id)})


class AdjustBody(_Body):
    sub_account_id: int
    currency: CurrencyCode
    amount: Money
    memo: str = Field(min_length=1, max_length=200)


@router.post("/adjustments")
def adjust(app: App, body: AdjustBody):
    app.accounts.adjust(body.sub_account_id, body.currency, body.amount, body.memo)
    return ok(_sub_view(app, app.accounts.get(body.sub_account_id)))


# ───────────── 수수료 ─────────────
@router.get("/fee-schedules")
def fee_list(app: App, broker_account_id: str | None = None):
    return ok(queries.fee_schedules(app.db, broker_account_id))


class FeeBody(_Body):
    broker_account_id: str
    market: MarketCode
    side: Literal["buy", "sell"]
    fee_rate: Money = Field(ge=0, lt=1)
    min_fee: Money = Field(default=Decimal("0"), ge=0)
    tax_rate: Money = Field(default=Decimal("0"), ge=0, lt=1)
    venue: str | None = None
    effective_from: date = date(2000, 1, 1)


@router.put("/fee-schedules")
def fee_put(app: App, body: FeeBody):
    """새 요율 행을 추가한다. 같은 조건에서는 effective_from 이 가장 늦은 행이 적용된다."""
    _alias(app, body.broker_account_id)
    app.fees.set_rule(body.broker_account_id, body.market, body.side,
                      FeeRule(body.fee_rate, body.min_fee, body.tax_rate), body.venue, body.effective_from)
    return ok(queries.fee_schedules(app.db, body.broker_account_id))


# ───────────── 대사 / 정지 ─────────────
class ReconcileBody(_Body):
    broker_account_id: str
    market: MarketCode
    halt_on_mismatch: bool = True


@router.post("/reconcile")
async def reconcile(app: App, body: ReconcileBody):
    _alias(app, body.broker_account_id)
    if body.market not in app.profiles[body.broker_account_id].markets:
        raise OmsError("MARKET_NOT_SUPPORTED", f"{body.broker_account_id} 계좌는 {body.market} 미지원")
    return ok(await app.reconciler.run(body.broker_account_id, body.market, halt_on_mismatch=body.halt_on_mismatch))


@router.get("/reconciliations")
def reconciliations(app: App, broker_account_id: str | None = None, limit: int = 50):
    return ok(queries.reconciliations(app.db, broker_account_id, limit))


class KillSwitchBody(_Body):
    scope: Literal["all", "broker", "broker_market", "sub", "market"]
    id: str = ""
    action: Literal["halt", "resume"]
    cancel_open_orders: bool = False


@router.post("/kill-switch")
async def kill_switch(app: App, body: KillSwitchBody):
    canceled = None
    if body.action == "halt":
        app.halt(body.scope, body.id)
        if body.cancel_open_orders:
            canceled = await app.cancel_open_orders(body.scope, body.id)
    else:
        app.resume(body.scope, body.id)
    return ok({"halted": app.halted(), "canceled": canceled})


# ───────────── 시세 ─────────────
@router.get("/market-data")
def market_data(app: App):
    """시세 허브 상태: 채널별 참조 수, 시장별 수신 계좌, 계좌별 실시간 등록 슬롯, 틱 저장 건수."""
    sessions = {a: b.health().get("realtime") for a, b in app.brokers.items()}
    return ok({**app.market.stats(), "sessions": sessions,
               "ticks_written": app.ticks.written if app.ticks else None})


# ───────────── 종목 ─────────────
class InstrumentBody(_Body):
    symbol: str = Field(description="KR:005930 / US:AAPL")
    exchange: Literal["KRX", "NASD", "NYSE", "AMEX"] | None = None
    name: str = Field(default="", max_length=80)
    nxt_tradable: bool = False
    kind: Literal["stock", "etf", "etn", "other"] = "stock"


@router.put("/instruments")
def instrument_put(app: App, body: InstrumentBody):
    """수동 등록/수정. 종목 마스터에 없는 종목이나 NXT 거래 가능 여부(마스터에 없음) 지정에 쓴다."""
    market, _, code = body.symbol.upper().partition(":")
    return ok(app.catalog.upsert(market, code, body.exchange, body.name, body.nxt_tradable, body.kind))


class MasterRefreshBody(_Body):
    markets: list[Literal["KR", "US"]] = Field(default_factory=lambda: ["KR", "US"])


@router.post("/instruments/refresh-master")
async def instrument_refresh_master(app: App, body: MasterRefreshBody | None = None):
    """KIS 종목 마스터 파일을 받아 일괄 적재 (국내 코스피/코스닥, 미국 나스닥/뉴욕/아멕스)."""
    markets = tuple(body.markets) if body else ("KR", "US")
    try:
        counts = await app.refresh_master(markets)
    except Exception as e:
        raise OmsError("BROKER_UNAVAILABLE", f"종목 마스터 다운로드 실패: {type(e).__name__}: {e}") from None
    return ok({"counts": counts})


@router.post("/calendar/refresh")
async def calendar_refresh(app: App):
    """국내 휴장일을 KIS 에서 즉시 다시 받는다 (평소에는 하루 한 번 자동)."""
    try:
        n = await app.calendar.refresh_kr(force=True)
    except BrokerUnavailable as e:
        raise OmsError("BROKER_UNAVAILABLE", str(e)) from None
    return ok({"days": n, "kr_holidays_ahead": sorted(d for d in app.hours.holidays["KR"] if d >= app.clock().date())})
