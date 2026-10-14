"""서브계좌 API (/me, X-API-Key) — docs/PLAN.md 8.3

모든 조회와 주문은 API 키에 묶인 서브계좌 하나로 한정된다.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from central.api.common import App, Money, Sub, ok
from central.core.money import ZERO, q_cash
from central.oms import queries
from central.oms.orders import PlaceOrder

router = APIRouter(prefix="/me", tags=["me"])


class _Body(BaseModel):
    model_config = {"extra": "forbid"}


def _period(start: datetime | date | None, end: datetime | date | None) -> tuple[datetime | None, datetime | None]:
    """날짜만 오면 그날 00:00 ~ 23:59:59 (KST) 로 해석."""
    if isinstance(start, date) and not isinstance(start, datetime):
        start = queries.day_start(start)
    if isinstance(end, date) and not isinstance(end, datetime):
        end = queries.day_end(end)
    return start, end


@router.get("")
def me(app: App, sub: Sub):
    profile = app.profiles[sub.broker_account_id]
    markets = [m for m in sub.markets if m in profile.markets]
    return ok({
        "id": sub.id, "name": sub.name, "type": sub.type, "status": sub.status, "fx_mode": sub.fx_mode,
        "broker_env": profile.env, "markets": markets,
        "trading_enabled": profile.trading_enabled and sub.status == "active",
        "kr_routes": profile.kr_routes if "KR" in markets else [],
        "kr_default_route": profile.kr_default_route if "KR" in markets else None,
        "halted": {m: app.risk.is_halted(sub.broker_account_id, sub.id, m) for m in markets},
        "risk_limits": queries.risk_limits(app.db, sub.id),
    })


@router.get("/balance")
async def balance(app: App, sub: Sub):
    balances = app.accounts.balances(sub.id)
    cost: dict[str, Decimal] = {}
    realized: dict[str, Decimal] = {}
    for p in app.accounts.positions(sub.id, include_closed=True):
        cost[p.currency] = cost.get(p.currency, ZERO) + q_cash(p.avg_price * p.qty, p.currency)
        realized[p.currency] = realized.get(p.currency, ZERO) + p.realized_pnl
    data: dict = {c: {"cash": b.cash, "held": b.held, "available": b.available,
                      "position_cost": cost.get(c, ZERO), "realized_pnl": realized.get(c, ZERO)}
                  for c, b in balances.items()}

    profile = app.profiles[sub.broker_account_id]
    if sub.fx_mode == "broker" and profile.integrated_margin:
        # 통합증거금 자동환전: KRW 주문가능금액을 USD 로 환산해 더한다 (홀드와 같은 환율 버퍼 적용)
        try:
            rate = await app.brokers[sub.broker_account_id].fx_rate()
            hold_rate = rate * (1 + app.orders.fx_buffer)
            krw_as_usd = (max(balances["KRW"].available, ZERO) / hold_rate).quantize(Decimal("0.01"))
            data["USD"]["orderable"] = max(balances["USD"].available, ZERO) + krw_as_usd
            data["fx_rate"] = rate
        except Exception:
            data["USD"]["orderable"] = None
    # 평가금액과 평가손익은 실시간 시세(M5) 연동 후 제공
    return ok(data)


@router.get("/positions")
def positions(app: App, sub: Sub, market: Literal["KR", "US"] | None = None):
    items = app.accounts.positions(sub.id)
    if market:
        items = [p for p in items if p.instrument_id.startswith(f"{market}:")]
    return ok([{"symbol": p.instrument_id, "currency": p.currency, "qty": p.qty, "sellable": p.sellable,
                "avg_price": p.avg_price, "realized_pnl": p.realized_pnl} for p in items])


@router.get("/ledger")
def ledger(app: App, sub: Sub, currency: Literal["KRW", "USD"] | None = None, type: str | None = None,
           start: datetime | date | None = Query(default=None, alias="from"),
           end: datetime | date | None = Query(default=None, alias="to"), limit: int = 200):
    s, e = _period(start, end)
    return ok(queries.ledger_entries(app.db, sub.id, currency=currency, type_=type, start=s, end=e, limit=limit))


@router.get("/pnl")
def pnl(app: App, sub: Sub, market: Literal["KR", "US"] | None = None,
        start: datetime | date | None = Query(default=None, alias="from"),
        end: datetime | date | None = Query(default=None, alias="to")):
    s, e = _period(start, end)
    return ok(queries.pnl(app.db, sub.id, market=market, start=s, end=e))


# ───────────── 주문 ─────────────
class OrderBody(_Body):
    client_order_id: str = Field(min_length=1, max_length=48)
    symbol: str
    side: Literal["buy", "sell"]
    order_type: Literal["limit", "market"] = "limit"
    qty: int = Field(gt=0)
    price: Money | None = None
    route: Literal["KRX", "NXT", "SOR"] | None = None


@router.post("/orders")
async def place(app: App, sub: Sub, body: OrderBody):
    inst = app.catalog.resolve(body.symbol)
    order = await app.orders.place(sub.id, PlaceOrder(
        body.client_order_id, inst.id, body.side, body.order_type, body.qty, body.price, body.route))
    return ok(order)


@router.get("/orders")
def order_list(app: App, sub: Sub, status: str | None = None, symbol: str | None = None,
               market: Literal["KR", "US"] | None = None,
               start: datetime | date | None = Query(default=None, alias="from"),
               end: datetime | date | None = Query(default=None, alias="to"), limit: int = 200):
    s, e = _period(start, end)
    return ok(queries.orders(app.db, sub.id, status=status, symbol=symbol, market=market, start=s, end=e, limit=limit))


@router.get("/orders/{order_id}")
def order_get(app: App, sub: Sub, order_id: int):
    order = app.orders.get(sub.id, order_id)
    return ok({**ok(order)["data"], "fills": ok(queries.fills(app.db, sub.id, order_id=order_id))["data"]})


@router.post("/orders/{order_id}/cancel")
async def order_cancel(app: App, sub: Sub, order_id: int):
    return ok(await app.orders.cancel(sub.id, order_id))


class AmendBody(_Body):
    price: Money
    qty: int | None = Field(default=None, gt=0)


@router.post("/orders/{order_id}/amend")
async def order_amend(app: App, sub: Sub, order_id: int, body: AmendBody):
    return ok(await app.orders.amend(sub.id, order_id, body.price, body.qty))


@router.post("/orders/cancel-all")
async def cancel_all(app: App, sub: Sub, market: Literal["KR", "US"] | None = None):
    return ok(await app.cancel_open_orders("sub", str(sub.id), market=market))


@router.get("/fills")
def fill_list(app: App, sub: Sub, symbol: str | None = None, order_id: int | None = None,
              start: datetime | date | None = Query(default=None, alias="from"),
              end: datetime | date | None = Query(default=None, alias="to"), limit: int = 200):
    s, e = _period(start, end)
    return ok(queries.fills(app.db, sub.id, symbol=symbol, order_id=order_id, start=s, end=e, limit=limit))
