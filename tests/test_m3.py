"""M3: KIS 브로커 요청 형식, 체결통보 해석, 주문 복구/동기화, 대사, 초기 동기화."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from central.broker.base import BrokerCancelled, BrokerFill, BrokerOrderRejected, BrokerReject, BrokerUnavailable
from central.broker.base import BrokerOrderRequest
from central.broker.kis.auth import KST, Token, TokenCache
from central.broker.kis.broker import KisBroker
from central.broker.kis.notice import KR_COLUMNS, US_COLUMNS, NoticeParser
from central.broker.kis.ws import RealtimeMessage
from central.oms.errors import OmsError
from central.oms.orders import PlaceOrder
from central.oms.sync import OrderSync
from central.reconcile.reconciler import Reconciler, bootstrap_from_broker
from central.settings import load_settings
from tests.oms_env import AAPL, ALIAS, KR_OPEN, SAMSUNG, make_env

D = Decimal
TODAY = date(2026, 10, 14)


def _record(columns: list[str], **values: str) -> list[str]:
    return [values.get(c, "") for c in columns]


# ───────────── 체결통보 해석 ─────────────

def test_kr_notice_fill_cancel_reject() -> None:
    p = NoticeParser("KR")
    fill = p.parse(_record(KR_COLUMNS, ODER_NO="0000123", SELN_BYOV_CLS="02", CNTG_YN="2", RFUS_YN="0",
                           STCK_SHRN_ISCD="005930", CNTG_QTY="3", CNTG_UNPR="61000", STCK_CNTG_HOUR="093015",
                           ORD_EXG_GB="2"), TODAY)
    assert isinstance(fill, BrokerFill)
    assert (fill.broker_order_no, fill.instrument_id, fill.side, fill.qty, fill.price, fill.venue) == (
        "0000123", "KR:005930", "buy", 3, D("61000"), None)  # ORD_EXG_GB 의미 미확정 -> 공통 규칙
    assert fill.filled_at == datetime(2026, 10, 14, 9, 30, 15, tzinfo=KST)

    again = p.parse(_record(KR_COLUMNS, ODER_NO="0000123", SELN_BYOV_CLS="02", CNTG_YN="2", STCK_SHRN_ISCD="005930",
                            CNTG_QTY="3", CNTG_UNPR="61000", STCK_CNTG_HOUR="093015"), TODAY)
    assert isinstance(again, BrokerFill) and again.fill_key != fill.fill_key

    cancel = p.parse(_record(KR_COLUMNS, ODER_NO="0000130", OODER_NO="0000123", RCTF_CLS="2", CNTG_YN="1",
                             RFUS_YN="0", ODER_QTY="7"), TODAY)
    assert cancel == BrokerCancelled("0000123", 7)

    reject = p.parse(_record(KR_COLUMNS, ODER_NO="0000140", CNTG_YN="1", RFUS_YN="1"), TODAY)
    assert isinstance(reject, BrokerOrderRejected)

    accepted = p.parse(_record(KR_COLUMNS, ODER_NO="0000150", CNTG_YN="1", RFUS_YN="0", RCTF_CLS="0"), TODAY)
    assert accepted is None


def test_us_notice_fill_price() -> None:
    p = NoticeParser("US")
    fill = p.parse(_record(US_COLUMNS, ODER_NO="30001", SELN_BYOV_CLS="01", CNTG_YN="2", STCK_SHRN_ISCD="AAPL",
                           CNTG_QTY="2", CNTG_UNPR="187.2500", CNTG_UNPR12="", STCK_CNTG_HOUR="103000"), TODAY)
    assert isinstance(fill, BrokerFill)
    assert (fill.instrument_id, fill.side, fill.qty, fill.price, fill.venue) == ("US:AAPL", "sell", 2, D("187.2500"), None)


def test_safe_view_excludes_personal_fields() -> None:
    p = NoticeParser("KR")
    d = p.to_dict(_record(KR_COLUMNS, CUST_ID="someone", ACNT_NO="12345678", ACNT_NAME="홍길동", ODER_NO="1"))
    view = p.safe_view(d)
    assert "CUST_ID" not in view and "ACNT_NO" not in view and "ACNT_NAME" not in view


# ───────────── KisBroker 요청 형식 (모의 HTTP) ─────────────

def _kis_broker(home: Path, handler, alias: str = "real") -> tuple[KisBroker, httpx.AsyncClient]:
    settings = load_settings()
    TokenCache(settings.tokens_dir, alias).save(
        Token("dummy-token", datetime.now(KST) + timedelta(hours=20), datetime.now(KST)))
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return KisBroker(settings.accounts[alias], http, settings.tokens_dir), http


async def test_kis_submit_kr_and_us_params(home: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"rt_cd": "0", "msg_cd": "APBK0013", "msg1": "주문 전송 완료",
                                         "output": {"ODNO": "0000777", "KRX_FWDG_ORD_ORGNO": "91252"}})

    broker, http = _kis_broker(home, handler)
    async with http:
        ack = await broker.submit(BrokerOrderRequest(SAMSUNG, "buy", "limit", 10, D("61000"), "SOR"))
        assert (ack.broker_order_no, ack.broker_org_no) == ("0000777", "91252")
        await broker.submit(BrokerOrderRequest(AAPL, "sell", "limit", 3, D("187.20"), None))

    kr = json.loads(seen[0].content)
    assert seen[0].headers["tr_id"] == "TTTC0012U"
    assert kr["EXCG_ID_DVSN_CD"] == "SOR" and kr["ORD_DVSN"] == "00" and kr["ORD_UNPR"] == "61000" and kr["PDNO"] == "005930"
    us = json.loads(seen[1].content)
    assert seen[1].headers["tr_id"] == "TTTT1006U"
    assert us["OVRS_EXCG_CD"] == "NASD" and us["OVRS_ORD_UNPR"] == "187.2" and us["SLL_TYPE"] == "00"


async def test_kis_error_mapping(home: Path) -> None:
    def reject(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"rt_cd": "1", "msg_cd": "APBK0952", "msg1": "주문가능금액을 초과"})

    broker, http = _kis_broker(home, reject)
    async with http:
        with pytest.raises(BrokerReject) as e:
            await broker.submit(BrokerOrderRequest(SAMSUNG, "buy", "limit", 1, D("61000"), "KRX"))
        assert e.value.code == "APBK0952"

    def timeout(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=req)

    broker, http = _kis_broker(home, timeout)
    async with http:
        with pytest.raises(BrokerUnavailable):
            await broker.submit(BrokerOrderRequest(SAMSUNG, "buy", "limit", 1, D("61000"), "KRX"))


async def test_kis_order_statuses_and_snapshot_parsing(home: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        tr_id = req.headers["tr_id"]
        if tr_id == "TTTC0081R":
            return httpx.Response(200, json={"rt_cd": "0", "output1": [
                {"odno": "0000777", "orgn_odno": "", "sll_buy_dvsn_cd": "02", "pdno": "005930", "ord_qty": "10",
                 "ord_unpr": "61000", "tot_ccld_qty": "4", "avg_prvs": "61000", "rmn_qty": "6", "ord_tmd": "093001"}]})
        if tr_id == "TTTC8434R":
            return httpx.Response(200, json={"rt_cd": "0",
                                             "output1": [{"pdno": "005930", "hldg_qty": "4", "pchs_avg_pric": "61000.0000"}],
                                             "output2": [{"prvs_rcdl_excc_amt": "756000"}]})
        if tr_id == "CTRP6504R":
            return httpx.Response(200, json={"rt_cd": "0", "output1": [
                {"pdno": "AAPL", "ccld_qty_smtl1": "5", "avg_unpr3": "187.1000"}], "output3": {}})
        if tr_id == "TTTC2101R":
            return httpx.Response(200, json={"rt_cd": "0", "output": [
                {"crcy_cd": "USD", "frcr_dncl_amt1": "1000.00", "ustl_buy_amt": "935.50", "ustl_sll_amt": "0",
                 "bass_exrt": "1395.20"}]})
        return httpx.Response(404)

    broker, http = _kis_broker(home, handler)
    async with http:
        st = (await broker.order_statuses("KR", TODAY))[0]
        assert (st.broker_order_no, st.instrument_id, st.side, st.filled_qty, st.open_qty) == ("0000777", "KR:005930", "buy", 4, 6)
        assert not st.closed_unfilled
        kr = await broker.snapshot("KR")
        assert kr.positions == {"KR:005930": (4, D("61000.0000"))} and kr.cash == {"KRW": D("756000")}
        us = await broker.snapshot("US")
        assert us.positions["US:AAPL"][0] == 5
        assert us.cash == {"USD": D("64.50")} and us.fx_rate == D("1395.20")
        assert await broker.fx_rate() == D("1395.20")


async def test_kis_notice_consume_emits_events(home: Path) -> None:
    broker, http = _kis_broker(home, lambda req: httpx.Response(404))
    got: list = []

    async def handler(ev) -> None:
        got.append(ev)

    broker.set_event_handler(handler)
    broker.session.set_handler("H0STCNI0", broker._on_notice)  # start() 가 하는 등록 (연결 없이)

    rec = _record(KR_COLUMNS, ODER_NO="1", SELN_BYOV_CLS="02", CNTG_YN="2", STCK_SHRN_ISCD="005930",
                  CNTG_QTY="1", CNTG_UNPR="61000", STCK_CNTG_HOUR="090001")
    await broker.session.dispatch(RealtimeMessage("H0STCNI0", True, [rec]))
    await broker.session.dispatch(RealtimeMessage("H0STCNT0", False, [["ignored"]]))  # 핸들러 없는 TR 은 무시
    await http.aclose()
    assert len(got) == 1 and isinstance(got[0], BrokerFill)


# ───────────── 주문 복구 / 동기화 (SimBroker) ─────────────

async def test_unknown_order_resolved_by_sync() -> None:
    env = make_env()
    env.fund("KRW", "1000000")
    a = env.new_sub("A", KRW="1000000")
    env.sim.drop_ack_next = True
    o = await env.orders.place(a, PlaceOrder("a1", SAMSUNG.id, "buy", "limit", 5, D("61000")))
    assert o.status == "UNKNOWN"

    env.sim.fill_silently(next(iter(env.sim.orders)), 2, D("61000"))   # 응답도, 체결통보도 유실
    report = await OrderSync(env.orders, clock=env.clock).run(ALIAS, "KR")
    assert report.resolved == [o.id] and report.synthetic_fills == 1
    view = env.orders.get(a, o.id)
    assert view.status == "PARTIALLY_FILLED" and view.filled_qty == 2 and view.broker_order_no
    env.reconcile()


async def test_unknown_order_not_at_broker_fails_after_timeout() -> None:
    env = make_env()
    env.fund("KRW", "1000000")
    a = env.new_sub("A", KRW="1000000")
    env.sim.fail_next = True
    o = await env.orders.place(a, PlaceOrder("a1", SAMSUNG.id, "buy", "limit", 5, D("61000")))
    sync = OrderSync(env.orders, clock=env.clock)
    await sync.run(ALIAS, "KR")
    assert env.orders.get(a, o.id).status == "UNKNOWN"          # 아직 대기
    env.clock.t = KR_OPEN + timedelta(minutes=3)
    report = await sync.run(ALIAS, "KR")
    assert report.failed == [o.id]
    assert env.orders.get(a, o.id).status == "SUBMIT_FAILED"
    assert env.accounts.balances(a)["KRW"].held == 0


async def test_ambiguous_unknown_left_for_manual() -> None:
    env = make_env()
    env.fund("KRW", "1000000")
    a = env.new_sub("A", KRW="1000000")
    env.sim.drop_ack_next = True
    o = await env.orders.place(a, PlaceOrder("a1", SAMSUNG.id, "buy", "limit", 1, D("61000")))
    # 같은 조건의 주문이 HTS 에서도 하나 더 들어온 상황
    await env.sim.submit(BrokerOrderRequest(SAMSUNG, "buy", "limit", 1, D("61000"), "KRX"))
    report = await OrderSync(env.orders, clock=env.clock).run(ALIAS, "KR")
    assert report.ambiguous == [o.id]
    assert env.orders.get(a, o.id).status == "UNKNOWN"
    assert any(e.type == "ORDER_AMBIGUOUS" for e in env.events)


async def test_sync_catches_missed_fill_and_cancel() -> None:
    env = make_env()
    env.fund("KRW", "1000000")
    a = env.new_sub("A", KRW="1000000")
    o = await env.orders.place(a, PlaceOrder("a1", SAMSUNG.id, "buy", "limit", 10, D("61000")))
    no = env.orders.get(a, o.id).broker_order_no
    assert no
    env.sim.fill(no, 3, D("61000"))
    await env.sim.drain()                     # 3주는 정상 통보
    env.sim.fill_silently(no, 4, D("60900"))  # 4주는 통보 유실
    await env.sim.cancel(no, "", SAMSUNG, 3)
    env.sim.pending_events.clear()            # 취소 통보도 유실
    report = await OrderSync(env.orders, clock=env.clock).run(ALIAS, "KR")
    assert report.synthetic_fills == 1 and report.cancels == 1
    view = env.orders.get(a, o.id)
    assert view.status == "CANCELED" and view.filled_qty == 7
    assert env.accounts.balances(a)["KRW"].held == 0
    env.reconcile()


# ───────────── 대사 / 초기 동기화 ─────────────

async def test_reconcile_ok_then_mismatch_halts() -> None:
    env = make_env()
    env.fund("KRW", "1000000")
    a = env.new_sub("A", KRW="1000000")
    rec = Reconciler(env.db, env.orders.bus, {ALIAS: env.sim}, env.risk, clock=env.clock)
    await env.orders.place(a, PlaceOrder("a1", SAMSUNG.id, "buy", "limit", 2, D("61000")))
    env.sim.set_price(SAMSUNG, D("61000"))
    await env.sim.drain()
    result = await rec.run(ALIAS, "KR")
    assert result.ok and result.cash_checked

    env.sim.external_fill(SAMSUNG, "buy", 1, D("61000"))   # 장부에 아직 반영 안 된 실계좌 변화
    result = await rec.run(ALIAS, "KR")
    assert not result.ok and any("수량" in d for d in result.diffs)
    with pytest.raises(OmsError) as e:
        await env.orders.place(a, PlaceOrder("a2", SAMSUNG.id, "buy", "limit", 1, D("61000")))
    assert e.value.code == "HALTED"


async def test_bootstrap_imports_existing_account() -> None:
    env = make_env()
    env.sim.deposit("KRW", D("5000000"))
    env.sim.deposit("USD", D("5000"))
    env.sim.positions["KR:005930"] = 10
    env.sim.avg["KR:005930"] = D("58000")
    env.sim.positions["US:AAPL"] = 3
    env.sim.avg["US:AAPL"] = D("170")
    summary = await bootstrap_from_broker(env.accounts, env.sim, ALIAS, ["KR", "US"])
    assert set(summary) == {"KR", "US"}
    un = env.unallocated_id
    assert env.cash(un, "KRW") == D("5000000") and env.cash(un, "USD") == D("5000")
    assert env.qty(un, "KR:005930") == 10 and env.qty(un, "US:AAPL") == 3
    env.reconcile()
    with pytest.raises(OmsError) as e:
        await bootstrap_from_broker(env.accounts, env.sim, ALIAS, ["KR"])
    assert e.value.code == "ALREADY_BOOTSTRAPPED"
