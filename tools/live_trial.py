"""M3 실거래 검증: 실제 KIS 계좌로 서브계좌 2개의 주문 흐름 전체를 1주 단위로 확인한다.

    python -m tools.live_trial --account paper --market KR
    python -m tools.live_trial --account real --market US --confirm-real     # 실제 돈이 움직임

흐름
 1. 체결통보 연결, 초기 동기화(미배정으로 실계좌 잔고 반영), 사전 대사
 2. 서브계좌 T-A, T-B 생성 (해당 시장 전용) 후 미배정에서 소액 배정
 3. T-A: 체결되지 않을 가격으로 1주 매수 → 접수 확인 → 취소
 4. T-B: 체결될 가격으로 1주 매수 → 체결 확인 → 1주 매도 → 체결 확인
 5. 주문 동기화, 사후 대사 (수수료 설정 전이면 현금 차이는 수수료/세금만큼 나는 것이 정상)

검증 전용 DB(CENTRAL_HOME/data/trial_<계좌>.db)를 쓰므로 운영 장부와 섞이지 않는다.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from central.app import CentralApp
from central.broker.kis.broker import KisBroker
from central.core.instruments import Instrument, InstrumentRegistry, tick_size
from central.core.logging import setup_logging
from central.oms.errors import OmsError
from central.oms.orders import PlaceOrder
from central.settings import load_settings
from central.storage.db import Database

SYMBOLS = {"KR": Instrument("KR:005930", "KR", "005930", "KRX", "삼성전자", nxt_tradable=True),
           "US": Instrument("US:AAPL", "US", "AAPL", "NASD", "Apple")}


def to_tick(inst: Instrument, price: Decimal, up: bool) -> Decimal:
    tick = tick_size(inst, price)
    return (price / tick).to_integral_value(ROUND_CEILING if up else ROUND_FLOOR) * tick


async def wait_status(app: CentralApp, sub_id: int, order_id: int, wanted: set[str], timeout: float) -> str:
    deadline = time.monotonic() + timeout
    status = ""
    while time.monotonic() < deadline:
        status = app.orders.get(sub_id, order_id).status
        if status in wanted:
            return status
        await asyncio.sleep(0.5)
    return status


def step(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'OK' if ok else 'FAIL':4}] {name:<34} {detail}", flush=True)
    return ok


async def run(args: argparse.Namespace) -> int:
    settings = load_settings()
    setup_logging(settings)
    alias, market = args.account, args.market
    account = settings.accounts[alias]
    if market not in account.profile.markets:
        print(f"{alias} 계좌는 {market} 미지원 ({account.profile.markets})")
        return 2
    if account.profile.env == "real" and not args.confirm_real:
        print("실전 계좌는 --confirm-real 이 필요합니다 (실제 주문이 나갑니다)")
        return 2

    # 검증 실행 동안만 이 계좌의 거래를 허용
    enabled = account.model_copy(update={"profile": account.profile.model_copy(update={"trading_enabled": True})})
    settings = settings.model_copy(update={"accounts": {alias: enabled}})
    inst = SYMBOLS[market]
    app = CentralApp(settings, db=Database.file(settings.data_dir / f"trial_{alias}.db"),
                     instruments=InstrumentRegistry([inst]))
    broker = app.brokers[alias]
    assert isinstance(broker, KisBroker)
    cur = inst.currency
    results: list[bool] = []
    print(f"\n=== 실거래 검증 [{alias}/{market}] {inst.id} ===")
    try:
        await app.start()
        try:
            await asyncio.wait_for(broker.notice_connected.wait(), 20)
        except asyncio.TimeoutError:
            pass
        results.append(step("체결통보 연결", broker.notice_connected.is_set()))

        boot = await app.bootstrap_if_empty(alias)
        results.append(step("초기 동기화", True, "이번에 실행" if boot else "이미 완료됨"))
        pre = await app.reconciler.run(alias, market, halt_on_mismatch=False)
        results.append(step("사전 대사", pre.ok, "; ".join(pre.diffs) or "일치"))

        quote = await broker.quote(inst)
        last = quote.last
        low = quote.lower_limit if (market == "KR" and quote.lower_limit) else to_tick(inst, last * Decimal("0.8"), up=False)
        # 국내 시장가 매수는 상한가로 홀드, 미국은 지정가(+1%). 수수료 여유 2%
        high = quote.upper_limit if (market == "KR" and quote.upper_limit) else to_tick(inst, last * Decimal("1.01"), up=True)
        budgets = {"T-A": (low * Decimal("1.02")).quantize(Decimal("0.01"), ROUND_CEILING),
                   "T-B": (high * Decimal("1.02")).quantize(Decimal("0.01"), ROUND_CEILING)}
        subs = {s.name: s for s in app.accounts.list(alias)}
        ids = {}
        for name, budget in budgets.items():
            sub = subs.get(name) or app.accounts.create_sub_account(alias, name, markets=[market])
            ids[name] = sub.id
            need = budget - app.accounts.balances(sub.id)[cur].available
            if need > 0:
                app.accounts.transfer(app.accounts.unallocated(alias).id, sub.id, cur, need)
        results.append(step("서브계좌 준비", True, f"T-A {budgets['T-A']}, T-B {budgets['T-B']} {cur} 이상"))

        tag = time.strftime("%H%M%S")
        # T-A: 체결되지 않을 가격 → 취소
        oa = await app.orders.place(ids["T-A"], PlaceOrder(f"trial-a-{tag}", inst.id, "buy", "limit", 1, low))
        results.append(step("T-A 지정가 접수", oa.status == "ACCEPTED", f"{low} {oa.status} {oa.reject_reason or ''}"))
        if oa.status == "ACCEPTED":
            await app.orders.cancel(ids["T-A"], oa.id)
            st = await wait_status(app, ids["T-A"], oa.id, {"CANCELED"}, 30)
            results.append(step("T-A 취소 (체결통보로 확정)", st == "CANCELED", st))

        # T-B: 체결될 가격 → 매수 후 매도
        if market == "KR":
            buy = PlaceOrder(f"trial-b1-{tag}", inst.id, "buy", "market", 1)
            sell = PlaceOrder(f"trial-b2-{tag}", inst.id, "sell", "market", 1)
        else:
            buy = PlaceOrder(f"trial-b1-{tag}", inst.id, "buy", "limit", 1, high)
            sell = None
        ob = await app.orders.place(ids["T-B"], buy)
        st = await wait_status(app, ids["T-B"], ob.id, {"FILLED", "REJECTED", "CANCELED"}, 60)
        filled = app.orders.get(ids["T-B"], ob.id)
        results.append(step("T-B 매수 체결", st == "FILLED", f"{st} 평균가 {filled.avg_fill_price}"))
        if st == "FILLED":
            if sell is None:
                q = await broker.quote(inst)
                sell = PlaceOrder(f"trial-b2-{tag}", inst.id, "sell", "limit", 1, to_tick(inst, q.last * Decimal("0.99"), up=False))
            os_ = await app.orders.place(ids["T-B"], sell)
            st = await wait_status(app, ids["T-B"], os_.id, {"FILLED", "REJECTED", "CANCELED"}, 60)
            sold = app.orders.get(ids["T-B"], os_.id)
            results.append(step("T-B 매도 체결", st == "FILLED", f"{st} 평균가 {sold.avg_fill_price}"))

        report = await app.sync.run(alias, market)
        results.append(step("주문 동기화", not (report.ambiguous or report.failed),
                            f"보정 체결 {report.synthetic_fills}, 취소 {report.cancels}"))
        post = await app.reconciler.run(alias, market, halt_on_mismatch=False)
        qty_ok = not any("수량" in d for d in post.diffs)
        results.append(step("사후 대사 (수량)", qty_ok, "; ".join(d for d in post.diffs if "수량" in d) or "일치"))
        cash_diffs = [d for d in post.diffs if "현금" in d]
        step("사후 대사 (현금)", not cash_diffs,
             (cash_diffs[0] + "  ← 수수료/세금 미설정이면 정상") if cash_diffs else "일치")

        for name, sid in ids.items():
            b = app.accounts.balances(sid)[cur]
            print(f"  {name}: {cur} 잔고 {b.cash} (홀드 {b.held}), 보유 {[(p.instrument_id, p.qty) for p in app.accounts.positions(sid)]}")
    except OmsError as e:
        results.append(step("OMS 오류", False, f"{e.code} {e.message}"))
    finally:
        await app.stop()

    passed = sum(results)
    print(f"\n결과: {passed}/{len(results)} 통과")
    return 0 if all(results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="M3 실거래 검증 (1주 단위)")
    parser.add_argument("--account", required=True)
    parser.add_argument("--market", choices=["KR", "US"], required=True)
    parser.add_argument("--confirm-real", action="store_true")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
