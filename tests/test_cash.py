"""대사 현금 차이 자동 처리: 미확인 입출금, 수수료 오차, 배당 (central/reconcile/cash.py)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

from central.broker.base import CashRight
from central.reconcile.cash import split
from tests.test_api import ADMIN, Env

DAY = date(2026, 10, 14)   # tests.oms_env.KR_OPEN 의 날짜


@pytest.fixture
async def env(home: Path):
    e = Env(home)
    e.fund("KRW", "1000000")
    await e.admin("PATCH", "/admin/broker-accounts/real", json={"trading_enabled": True})
    yield e
    await e.http.aclose()
    await e.app.http.aclose()


async def reconcile(env: Env) -> dict:
    return await env.admin("POST", "/admin/reconcile", json={"broker_account_id": "real", "market": "KR"})


def ledger_types(env: Env, sub_id: int) -> list[tuple[str, str]]:
    from sqlalchemy import select

    from central.storage.models import LedgerRow
    with env.app.db.session() as s:
        return [(r.type, str(r.amount)) for r in s.scalars(
            select(LedgerRow).where(LedgerRow.sub_account_id == sub_id).order_by(LedgerRow.id))]


async def buy_fill(env: Env, key: str, qty: int, price: str) -> None:
    async with env.sdk(key) as cs:
        o = await cs.buy("KR:005930", qty=qty, price=price)
        env.sim.fill(o["broker_order_no"], qty, D(price))
        await env.sim.drain()


def test_split_keeps_total() -> None:
    assert split(D("100"), {1: D(1), 2: D(1), 3: D(1)}, "KRW") == {1: D("34"), 2: D("33"), 3: D("33")}
    assert sum(split(D("-0.07"), {1: D(3), 2: D(1)}, "USD").values()) == D("-0.07")


async def test_unreported_deposit_and_withdrawal(env: Env) -> None:
    un = env.app.accounts.unallocated("real").id
    env.sim.deposit("KRW", D("100000"))                  # 앱에 알리지 않은 입금
    res = await reconcile(env)
    assert res["ok"] and "미확인 입금 KRW 100000" in res["resolved"][0]
    assert ledger_types(env, un)[-1] == ("UNIDENTIFIED", "100000")
    assert (await reconcile(env))["resolved"] == []      # 다시 돌려도 중복 기록 없음

    env.sim.deposit("KRW", D("-30000"))                  # 알리지 않은 출금 (미배정 안)
    res = await reconcile(env)
    assert res["ok"] and "미확인 출금 KRW 30000" in res["resolved"][0]

    env.sim.deposit("KRW", D("-5000000"))                # 미배정보다 큰 출금: 누구 돈인지 모름 → 정지
    res = await reconcile(env)
    assert not res["ok"] and any("미배정 주문 가능 현금보다 큼" in d for d in res["diffs"])
    assert ("broker_market", "real:KR") in env.app.risk.halted


async def test_fee_model_gap_charged_to_traders(env: Env) -> None:
    sub_id, key = await env.new_sub("A", ["KR"], KRW="800000")
    await buy_fill(env, key, 10, "61000")
    env.sim.deposit("KRW", D("-86"))                     # 실제 수수료가 모델(0)보다 86원 많음 (610000 의 1.4bp)
    res = await reconcile(env)
    assert res["ok"] and res["resolved"][0].startswith("수수료 보정 KRW -86")
    assert ledger_types(env, sub_id)[-1] == ("FEE", "-86")


async def test_big_gap_with_fills_is_not_fee(env: Env) -> None:
    sub_id, key = await env.new_sub("A", ["KR"], KRW="800000")
    await buy_fill(env, key, 10, "61000")
    env.sim.deposit("KRW", D("50000"))                   # 체결대금의 5bp 를 넘음 → 미확인 입금
    res = await reconcile(env)
    assert "미확인 입금 KRW 50000" in res["resolved"][0]
    assert ledger_types(env, sub_id)[-1][0] == "BUY"


async def test_dividend_allocated_to_holders(env: Env) -> None:
    a, key_a = await env.new_sub("A", ["KR"], KRW="400000")
    b, key_b = await env.new_sub("B", ["KR"], KRW="400000")
    await buy_fill(env, key_a, 3, "61000")
    await buy_fill(env, key_b, 1, "61000")
    env.sim.pay_right(CashRight("KR:005930", DAY, 4, D("1444"), D("222")), "KRW", DAY)   # 세후 1222
    env.sim.deposit("KRW", D("10000"))                   # 같은 날 입금도 섞임
    res = await reconcile(env)
    assert res["ok"], res
    assert res["resolved"][0] == "배당 KR:005930 KRW 1222 → #%d 917, #%d 305" % (a, b) \
        or res["resolved"][0] == "배당 KR:005930 KRW 1222 → #%d 916, #%d 306" % (a, b)
    assert "미확인 입금 KRW 10000" in res["resolved"][1]
    assert ledger_types(env, a)[-1][0] == "DIVIDEND" and ledger_types(env, b)[-1][0] == "DIVIDEND"


async def test_dividend_with_unknown_holdings_goes_unallocated_then_admin_assigns(env: Env) -> None:
    a, key_a = await env.new_sub("A", ["KR"], KRW="400000")
    await buy_fill(env, key_a, 2, "61000")
    env.sim.pay_right(CashRight("KR:005930", DAY, 5, D("1000"), D("0")), "KRW", DAY)    # 실계좌 기준일 수량 5 ≠ 장부 2
    res = await reconcile(env)
    assert res["ok"] and "미배정으로" in res["resolved"][0]
    un = env.app.accounts.unallocated("real").id
    assert ledger_types(env, un)[-1] == ("UNIDENTIFIED", "1000")

    out = await env.admin("POST", "/admin/dividends", json={
        "broker_account_id": "real", "symbol": "KR:005930", "amount": "1000", "source": "unidentified"})
    assert out["allocated"] == {str(a): "1000"}
    assert ledger_types(env, un)[-1] == ("UNIDENTIFIED", "-1000")
    assert (await reconcile(env))["ok"]
    res = await env.http.post("/admin/dividends", headers=ADMIN, json={
        "broker_account_id": "real", "symbol": "KR:005930", "amount": "1", "source": "unidentified"})
    assert res.status_code == 400 and res.json()["error"]["code"] == "INSUFFICIENT_CASH"


async def test_record_date_holdings_rewind_later_fills(env: Env) -> None:
    a, key_a = await env.new_sub("A", ["KR"], KRW="400000")
    await buy_fill(env, key_a, 2, "61000")
    env.sim.pay_right(CashRight("KR:005930", date(2026, 10, 13), 0, D("500"), D("0")), "KRW", DAY)
    res = await reconcile(env)            # 기준일(10-13)에는 보유가 없었음 → 배정 못 하고 미배정
    assert "미배정으로" in res["resolved"][0]


async def test_no_auto_when_positions_differ_or_disabled(env: Env) -> None:
    env.sim.positions["KR:000660"] = 1                   # 장부에 없는 보유 (HTS 매수 등)
    env.sim.deposit("KRW", D("100000"))
    res = await reconcile(env)
    assert not res["ok"] and res["resolved"] == []
    env.sim.positions["KR:000660"] = 0
    env.app.reconciler.auto_cash = False
    await env.admin("POST", "/admin/kill-switch", json={"scope": "broker_market", "id": "real:KR", "action": "resume"})
    res = await reconcile(env)
    assert not res["ok"] and res["diffs"] == ["KRW 현금 장부-실계좌 차이 -100000"]
