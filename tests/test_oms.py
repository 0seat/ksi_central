"""서브계좌 코어(M2) 시나리오 테스트. SimBroker 로 실계좌를 흉내 내고 매 단계 대사한다."""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal

import pytest

from central.broker.base import BrokerReject
from central.core.market_hours import KST
from central.oms.errors import OmsError
from central.oms.fees import FeeRule
from central.oms.orders import PlaceOrder
from central.risk.engine import RiskLimits
from tests.oms_env import AAPL, ALIAS, HYNIX, SAMSUNG, SMALL, US_OPEN, make_env

D = Decimal


def buy(cid: str, symbol: str, qty: int, price: str | None, order_type: str = "limit", route: str | None = None) -> PlaceOrder:
    return PlaceOrder(cid, symbol, "buy", order_type, qty, D(price) if price else None, route)


def sell(cid: str, symbol: str, qty: int, price: str) -> PlaceOrder:
    return PlaceOrder(cid, symbol, "sell", "limit", qty, D(price))


# ───────────── 서브계좌 / 자금 ─────────────

def test_unallocated_created_and_transfers() -> None:
    env = make_env()
    env.fund("KRW", "10000000")
    a = env.new_sub("A", KRW="3000000")
    assert env.cash(a, "KRW") == D("3000000")
    assert env.cash(env.unallocated_id, "KRW") == D("7000000")
    with pytest.raises(OmsError) as e:
        env.accounts.transfer(a, env.unallocated_id, "KRW", D("3000001"))
    assert e.value.code == "INSUFFICIENT_CASH"
    env.reconcile()


def test_system_and_suspended_sub_cannot_trade() -> None:
    env = make_env()
    env.fund("KRW", "1000000")
    a = env.new_sub("A", KRW="1000000")

    async def run() -> None:
        with pytest.raises(OmsError) as e:
            await env.orders.place(env.unallocated_id, buy("u1", SAMSUNG.id, 1, "61000"))
        assert e.value.code == "INVALID_REQUEST"
        env.accounts.set_status(a, "suspended")
        with pytest.raises(OmsError) as e:
            await env.orders.place(a, buy("a1", SAMSUNG.id, 1, "61000"))
        assert e.value.code == "SUB_ACCOUNT_SUSPENDED"

    asyncio.run(run())


# ───────────── 기본 매매와 격리 ─────────────

async def test_isolated_strategies_kr_us() -> None:
    """A(국내), B(미국)가 같은 실계좌에서 독립적으로 매매하고, C 는 남의 주식을 팔 수 없다."""
    env = make_env()
    env.fund("KRW", "10000000")
    env.fund("USD", "2000")
    a = env.new_sub("A", KRW="3000000")
    b = env.new_sub("B", USD="1000")
    c = env.new_sub("C", KRW="2000000")

    o = await env.orders.place(a, buy("a1", SAMSUNG.id, 10, "61000"))
    assert o.status == "ACCEPTED" and o.route == "SOR"
    assert env.available(a, "KRW") == D("2390000")   # 61만원 홀드
    env.sim.set_price(SAMSUNG, D("61000"))
    await env.sim.drain()
    assert env.orders.get(a, o.id).status == "FILLED"
    assert env.qty(a, SAMSUNG.id) == 10
    assert env.cash(a, "KRW") == D("2390000")
    assert env.available(a, "KRW") == D("2390000")   # 홀드 해제

    env.clock.t = US_OPEN
    ob = await env.orders.place(b, buy("b1", AAPL.id, 3, "187.25"))
    env.sim.set_price(AAPL, D("187.00"))              # 지정가보다 낮게 체결
    await env.sim.drain()
    assert env.orders.get(b, ob.id).avg_fill_price == D("187.0000")
    assert env.cash(b, "USD") == D("439.00")
    assert env.qty(b, AAPL.id) == 3

    env.clock.t = datetime(2026, 10, 15, 10, 0, tzinfo=KST)
    with pytest.raises(OmsError) as e:
        await env.orders.place(c, sell("c1", SAMSUNG.id, 1, "62000"))
    assert e.value.code == "INSUFFICIENT_POSITION"    # 실계좌엔 10주 있지만 C 소유가 아님
    env.reconcile()


async def test_sell_and_realized_pnl_with_fees() -> None:
    env = make_env()
    env.fees.set_rule(ALIAS, "KR", "buy", FeeRule(fee_rate=D("0.00015")))
    env.fees.set_rule(ALIAS, "KR", "sell", FeeRule(fee_rate=D("0.00015"), tax_rate=D("0.0018")))
    env.fund("KRW", "1000000")
    a = env.new_sub("A", KRW="1000000")

    await env.orders.place(a, buy("a1", SAMSUNG.id, 10, "61000"))
    env.sim.set_price(SAMSUNG, D("61000"))
    await env.sim.drain()
    # 610,000 + 수수료 91(원 미만 절사)
    assert env.cash(a, "KRW") == D("1000000") - D("610000") - D("91")

    await env.orders.place(a, sell("a2", SAMSUNG.id, 4, "62000"))
    assert env.accounts.positions(a)[0].sellable == 6
    env.sim.set_price(SAMSUNG, D("62000"))
    await env.sim.drain()
    pos = env.accounts.positions(a)[0]
    assert pos.qty == 6 and pos.sellable == 6
    # 매도 248,000 - 수수료 37 - 세금 446 / 실현손익 = 4,000 - 37 - 446
    assert pos.realized_pnl == D("3517")
    env.reconcile()


async def test_concurrent_orders_cannot_overspend() -> None:
    env = make_env()
    env.fund("KRW", "1000000")
    a = env.new_sub("A", KRW="1000000")
    results = await asyncio.gather(
        *(env.orders.place(a, buy(f"a{i}", SAMSUNG.id, 5, "61000")) for i in range(5)),
        return_exceptions=True,
    )
    ok = [r for r in results if not isinstance(r, Exception)]
    errors = [r for r in results if isinstance(r, OmsError)]
    assert len(ok) == 3                                # 30.5만 x 3 = 91.5만 ≤ 100만
    assert {e.code for e in errors} == {"INSUFFICIENT_CASH"}
    assert env.available(a, "KRW") == D("85000")


# ───────────── 통합증거금 ─────────────

async def test_integrated_margin_us_buy_with_krw_only() -> None:
    env = make_env(at=US_OPEN)
    env.sim.fx = D("1400")
    env.fund("KRW", "5000000")
    c = env.new_sub("C", fx_mode="broker", KRW="1000000")

    o = await env.orders.place(c, buy("c1", AAPL.id, 2, "187.25"))
    # 374.50 USD x 1400 x 1.02 = 534,786 KRW 홀드
    assert env.accounts.balances(c)["KRW"].held == D("534786")
    env.sim.set_price(AAPL, D("187.25"))
    await env.sim.drain()

    assert env.orders.get(c, o.id).status == "FILLED"
    assert env.cash(c, "USD") == D("0.00")
    assert env.cash(c, "KRW") == D("1000000") - D("524300")   # 374.50 x 1400
    assert env.accounts.balances(c)["KRW"].held == 0
    env.reconcile()


async def test_integrated_margin_partial_usd() -> None:
    env = make_env(at=US_OPEN)
    env.fund("KRW", "5000000")
    env.fund("USD", "100")
    c = env.new_sub("C", fx_mode="broker", KRW="1000000", USD="100")
    await env.orders.place(c, buy("c1", AAPL.id, 1, "187.25"))
    bal = env.accounts.balances(c)
    assert bal["USD"].held == D("100.00")
    assert bal["KRW"].held == D("124593")            # 87.25 x 1400 x 1.02 = 124,593 (올림)
    env.sim.set_price(AAPL, D("187.25"))
    await env.sim.drain()
    assert env.cash(c, "USD") == D("0.00")
    env.reconcile()


async def test_without_integrated_margin_usd_required() -> None:
    env = make_env(integrated_margin=False, at=US_OPEN)
    env.fund("KRW", "5000000")
    c = env.new_sub("C", fx_mode="broker", KRW="1000000")
    with pytest.raises(OmsError) as e:
        await env.orders.place(c, buy("c1", AAPL.id, 1, "187.25"))
    assert e.value.code == "INSUFFICIENT_CASH"


# ───────────── 서브계좌 정책 (markets, fx_mode) ─────────────

async def test_fx_mode_none_keeps_currencies_independent() -> None:
    """기본 정책: 실계좌가 통합증거금이어도 서브계좌는 KRW 로 USD 부족분을 메우지 않는다."""
    env = make_env(at=US_OPEN)
    env.fund("KRW", "5000000")
    c = env.new_sub("C", KRW="1000000")
    with pytest.raises(OmsError) as e:
        await env.orders.place(c, buy("c1", AAPL.id, 1, "187.25"))
    assert e.value.code == "INSUFFICIENT_CASH"


async def test_sub_account_market_scope() -> None:
    env = make_env()
    env.fund("KRW", "1000000")
    env.fund("USD", "1000")
    kr = env.new_sub("KR-only", markets=["KR"], KRW="1000000")
    us = env.new_sub("US-only", markets=["US"], USD="1000")
    with pytest.raises(OmsError) as e:
        await env.orders.place(us, buy("u1", SAMSUNG.id, 1, "61000"))
    assert e.value.code == "MARKET_NOT_SUPPORTED"
    env.clock.t = US_OPEN
    with pytest.raises(OmsError) as e:
        await env.orders.place(kr, buy("k1", AAPL.id, 1, "187.25"))
    assert e.value.code == "MARKET_NOT_SUPPORTED"
    with pytest.raises(OmsError):
        env.accounts.create_sub_account(ALIAS, "bad", markets=["JP"])
    with pytest.raises(OmsError):
        env.accounts.create_sub_account(ALIAS, "bad2", fx_mode="pool")


async def test_usd_inventory_in_unallocated_no_fx() -> None:
    """운용 방식: 미배정이 USD 재고(5,000달러)를 들고, 미국 전용 서브계좌는 USD 만 쓴다. 환전 기록이 없어야 한다."""
    env = make_env(at=US_OPEN)
    env.fund("KRW", "10000000")
    env.fund("USD", "5000")
    us = env.new_sub("US-trend", markets=["US"], USD="2000")
    kr = env.new_sub("KR-scalp", markets=["KR"], KRW="3000000")
    assert env.cash(env.unallocated_id, "USD") == D("3000.00")

    await env.orders.place(us, buy("u1", AAPL.id, 10, "187.25"))
    env.sim.set_price(AAPL, D("187.25"))
    await env.sim.drain()
    await env.orders.place(us, sell("u2", AAPL.id, 10, "190.00"))
    env.sim.set_price(AAPL, D("190.00"))
    await env.sim.drain()
    assert env.cash(us, "USD") == D("2027.50")

    env.clock.t = datetime(2026, 10, 15, 10, 0, tzinfo=KST)
    await env.orders.place(kr, buy("k1", SAMSUNG.id, 10, "61000"))
    env.sim.set_price(SAMSUNG, D("61000"))
    await env.sim.drain()

    env.accounts.transfer(us, env.unallocated_id, "USD", D("2027.50"))   # 운용 종료, USD 재고로 회수
    assert env.cash(env.unallocated_id, "USD") == D("5027.50")
    from central.storage.models import LedgerRow
    from sqlalchemy import select
    with env.db.session() as s:
        assert not list(s.scalars(select(LedgerRow).where(LedgerRow.type.in_(["FX", "FX_AUTO"]))))
    env.reconcile()


# ───────────── 부분체결 / 취소 / 정정 / 만료 ─────────────

async def test_partial_fill_then_cancel_releases_rest() -> None:
    env = make_env()
    env.fund("KRW", "1000000")
    a = env.new_sub("A", KRW="1000000")
    o = await env.orders.place(a, buy("a1", SAMSUNG.id, 10, "61000"))
    env.sim.fill(next(iter(env.sim.orders)), 4, D("61000"))
    await env.sim.drain()
    assert env.orders.get(a, o.id).status == "PARTIALLY_FILLED"
    assert env.accounts.balances(a)["KRW"].held == D("366000")

    c = await env.orders.cancel(a, o.id)
    assert c.status == "CANCEL_PENDING"
    await env.sim.drain()
    final = env.orders.get(a, o.id)
    assert final.status == "CANCELED" and final.filled_qty == 4
    assert env.accounts.balances(a)["KRW"].held == 0
    assert env.cash(a, "KRW") == D("756000")
    env.reconcile()


async def test_amend_price_up_reholds() -> None:
    env = make_env()
    env.fund("KRW", "700000")
    a = env.new_sub("A", KRW="700000")
    o = await env.orders.place(a, buy("a1", SAMSUNG.id, 10, "60000"))
    new = await env.orders.amend(a, o.id, D("61000"))
    assert env.orders.get(a, o.id).status == "REPLACED"
    assert new.status == "ACCEPTED" and new.orig_order_id == o.id and new.client_order_id == "a1#A1"
    assert env.accounts.balances(a)["KRW"].held == D("610000")
    with pytest.raises(OmsError) as e:
        await env.orders.amend(a, new.id, D("71000"))   # 71만 > 70만
    assert e.value.code == "INSUFFICIENT_CASH"
    env.sim.set_price(SAMSUNG, D("61000"))
    await env.sim.drain()
    assert env.orders.get(a, new.id).status == "FILLED"
    env.reconcile()


async def test_expire_at_close() -> None:
    env = make_env()
    env.fund("KRW", "1000000")
    a = env.new_sub("A", KRW="1000000")
    o = await env.orders.place(a, buy("a1", SAMSUNG.id, 1, "50000"))
    assert env.orders.expire_open_orders(ALIAS, "KR") == 1
    assert env.orders.get(a, o.id).status == "EXPIRED"
    assert env.accounts.balances(a)["KRW"].held == 0


# ───────────── 이상 상황 ─────────────

async def test_broker_reject_releases_hold() -> None:
    env = make_env()
    env.fund("KRW", "1000000")
    a = env.new_sub("A", KRW="1000000")
    env.sim.reject_next = BrokerReject("APBK0013", "주문가능금액 부족")
    o = await env.orders.place(a, buy("a1", SAMSUNG.id, 1, "61000"))
    assert o.status == "REJECTED" and "APBK0013" in (o.reject_reason or "")
    assert env.accounts.balances(a)["KRW"].held == 0


async def test_broker_timeout_marks_unknown_and_keeps_hold() -> None:
    env = make_env()
    env.fund("KRW", "1000000")
    a = env.new_sub("A", KRW="1000000")
    env.sim.fail_next = True
    o = await env.orders.place(a, buy("a1", SAMSUNG.id, 1, "61000"))
    assert o.status == "UNKNOWN"
    assert env.accounts.balances(a)["KRW"].held == D("61000")  # 접수됐을 수 있으므로 유지 (M3 복구에서 확정)
    assert any(e.type == "ORDER_UNKNOWN" for e in env.events)


async def test_unmatched_fill_goes_to_unallocated() -> None:
    env = make_env()
    env.fund("KRW", "1000000")
    env.sim.external_fill(SAMSUNG, "buy", 2, D("61000"))
    await env.sim.drain()
    assert env.qty(env.unallocated_id, SAMSUNG.id) == 2
    assert any(e.type == "UNMATCHED_FILL" for e in env.events)
    env.reconcile()


async def test_duplicate_fill_notice_ignored() -> None:
    env = make_env()
    env.fund("KRW", "1000000")
    a = env.new_sub("A", KRW="1000000")
    await env.orders.place(a, buy("a1", SAMSUNG.id, 2, "61000"))
    env.sim.set_price(SAMSUNG, D("61000"))
    dup = env.sim.pending_events[0]
    env.sim.pending_events.append(dup)
    await env.sim.drain()
    assert env.qty(a, SAMSUNG.id) == 2
    env.reconcile()


async def test_idempotent_client_order_id() -> None:
    env = make_env()
    env.fund("KRW", "1000000")
    a = env.new_sub("A", KRW="1000000")
    o1 = await env.orders.place(a, buy("same", SAMSUNG.id, 1, "61000"))
    o2 = await env.orders.place(a, buy("same", SAMSUNG.id, 1, "61000"))
    assert o1.id == o2.id
    assert len(env.sim.orders) == 1


# ───────────── 검증 규칙 ─────────────

@pytest.mark.parametrize(
    "req, at, code",
    [
        (buy("x", SAMSUNG.id, 1, "61010"), None, "INVALID_TICK_SIZE"),          # 5만~20만 구간 100원 단위
        (buy("x", SAMSUNG.id, 1, "61000"), datetime(2026, 10, 14, 8, 40, tzinfo=KST), "MARKET_CLOSED"),  # 장전 동시호가
        (buy("x", SAMSUNG.id, 1, "61000"), datetime(2026, 10, 17, 10, 0, tzinfo=KST), "MARKET_CLOSED"),  # 토요일
        (buy("x", AAPL.id, 1, None, order_type="market"), US_OPEN, "UNSUPPORTED_ORDER_TYPE"),
        (buy("x", AAPL.id, 1, "187.255"), US_OPEN, "INVALID_TICK_SIZE"),
        (buy("x", SMALL.id, 1, "10000", route="NXT"), None, "UNSUPPORTED_ROUTE"),
        (buy("x", "KR:999999", 1, "1000"), None, "INVALID_SYMBOL"),
        (buy("x", AAPL.id, 1, "187.25", route="SOR"), US_OPEN, "UNSUPPORTED_ROUTE"),
    ],
)
async def test_validation(req: PlaceOrder, at: datetime | None, code: str) -> None:
    env = make_env(at=at) if at else make_env()
    env.fund("KRW", "1000000")
    a = env.new_sub("A", KRW="1000000")
    with pytest.raises(OmsError) as e:
        await env.orders.place(a, req)
    assert e.value.code == code


async def test_closing_auction_allowed_by_default() -> None:
    env = make_env(at=datetime(2026, 10, 14, 15, 25, tzinfo=KST))
    env.fund("KRW", "1000000")
    a = env.new_sub("A", KRW="1000000")
    o = await env.orders.place(a, buy("a1", SAMSUNG.id, 1, "61000"))
    assert o.status == "ACCEPTED"


async def test_kr_market_order_holds_upper_limit() -> None:
    env = make_env()
    env.fund("KRW", "1000000")
    a = env.new_sub("A", KRW="1000000")
    o = await env.orders.place(a, buy("a1", SAMSUNG.id, 10, None, order_type="market"))
    assert env.accounts.balances(a)["KRW"].held == D("793000")   # 상한가 79,300 x 10
    env.sim.set_price(SAMSUNG, D("61000"))
    await env.sim.drain()
    assert env.orders.get(a, o.id).status == "FILLED"
    assert env.cash(a, "KRW") == D("390000")
    env.reconcile()


async def test_trading_disabled_account() -> None:
    env = make_env(trading_enabled=False)
    env.fund("KRW", "1000000")
    a = env.new_sub("A", KRW="1000000")
    with pytest.raises(OmsError) as e:
        await env.orders.place(a, buy("a1", SAMSUNG.id, 1, "61000"))
    assert e.value.code == "ACCOUNT_TRADING_DISABLED"


# ───────────── 리스크 ─────────────

async def test_risk_limits_and_halt() -> None:
    env = make_env()
    env.fund("KRW", "10000000")
    a = env.new_sub("A", KRW="10000000")
    env.risk.set_limits(a, "KR", RiskLimits(max_order_amount=D("1000000"), max_open_orders=2,
                                           allowed_symbols=[SAMSUNG.id, HYNIX.id]))
    with pytest.raises(OmsError) as e:
        await env.orders.place(a, buy("a1", SAMSUNG.id, 20, "61000"))   # 122만
    assert e.value.code == "RISK_LIMIT_EXCEEDED"
    with pytest.raises(OmsError):
        await env.orders.place(a, buy("a2", SMALL.id, 1, "10000"))
    await env.orders.place(a, buy("a3", SAMSUNG.id, 1, "60000"))
    await env.orders.place(a, buy("a4", SAMSUNG.id, 1, "60000"))
    with pytest.raises(OmsError) as e:
        await env.orders.place(a, buy("a5", SAMSUNG.id, 1, "60000"))
    assert "미체결" in e.value.message

    env.risk.halt("sub", str(a))
    with pytest.raises(OmsError) as e:
        await env.orders.place(a, sell("a6", SAMSUNG.id, 1, "60000"))
    assert e.value.code == "HALTED"


async def test_daily_loss_blocks_buys_not_sells() -> None:
    env = make_env()
    env.fund("KRW", "10000000")
    a = env.new_sub("A", KRW="10000000")
    env.risk.set_limits(a, "KR", RiskLimits(max_daily_loss=D("50000")))
    await env.orders.place(a, buy("a1", SAMSUNG.id, 20, "61000"))
    env.sim.set_price(SAMSUNG, D("61000"))
    await env.sim.drain()
    await env.orders.place(a, sell("a2", SAMSUNG.id, 10, "55000"))     # -6만 실현
    env.sim.set_price(SAMSUNG, D("55000"))
    await env.sim.drain()
    with pytest.raises(OmsError) as e:
        await env.orders.place(a, buy("a3", SAMSUNG.id, 1, "55000"))
    assert e.value.code == "RISK_LIMIT_EXCEEDED"
    o = await env.orders.place(a, sell("a4", SAMSUNG.id, 10, "55000"))  # 축소는 허용
    assert o.status == "ACCEPTED"


# ───────────── 종합: 3개 서브계좌 동시 운용 ─────────────

async def test_three_strategies_end_to_end_reconcile() -> None:
    env = make_env()
    env.fees.set_rule(ALIAS, "KR", "buy", FeeRule(fee_rate=D("0.00015")))
    env.fees.set_rule(ALIAS, "KR", "sell", FeeRule(fee_rate=D("0.00015"), tax_rate=D("0.0018")))
    env.fees.set_rule(ALIAS, "US", "buy", FeeRule(fee_rate=D("0.0025")))
    env.fees.set_rule(ALIAS, "US", "sell", FeeRule(fee_rate=D("0.0025")))
    env.fund("KRW", "20000000")
    env.fund("USD", "3000")
    a = env.new_sub("A", KRW="5000000")
    b = env.new_sub("B", USD="2000")
    c = env.new_sub("C", fx_mode="broker", KRW="5000000")

    # 국내: A, C 가 동시에 같은 종목 매수, A 는 일부 매도
    await asyncio.gather(
        env.orders.place(a, buy("a1", SAMSUNG.id, 30, "61000")),
        env.orders.place(c, buy("c1", SAMSUNG.id, 20, "61000")),
        env.orders.place(c, buy("c2", HYNIX.id, 5, "180000")),
    )
    env.sim.set_price(SAMSUNG, D("60900"))
    env.sim.set_price(HYNIX, D("180000"))
    await env.sim.drain()
    await env.orders.place(a, sell("a2", SAMSUNG.id, 10, "61500"))
    env.sim.set_price(SAMSUNG, D("61500"))
    await env.sim.drain()
    env.reconcile()

    # 미국: B 는 USD 로, C 는 통합증거금(KRW)으로 매수
    env.clock.t = US_OPEN
    await asyncio.gather(
        env.orders.place(b, buy("b1", AAPL.id, 5, "187.25")),
        env.orders.place(c, buy("c3", AAPL.id, 3, "187.25")),
    )
    env.sim.set_price(AAPL, D("187.10"))
    await env.sim.drain()
    env.reconcile()

    assert env.qty(a, SAMSUNG.id) == 20
    assert env.qty(c, SAMSUNG.id) == 20
    assert env.qty(b, AAPL.id) == 5 and env.qty(c, AAPL.id) == 3
    for sub in (a, b, c):
        assert all(bal.held == 0 for bal in env.accounts.balances(sub).values())
