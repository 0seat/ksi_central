"""웹 대시보드 (/ui): 로그인 세션, 권한 범위, 위젯 데이터, 레이아웃 저장. 호출 통계와 로그 버퍼."""

from __future__ import annotations

import logging
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest

from central.api import ui
from central.core.callstats import CallStats, classify
from central.core.logbuffer import LogBuffer
from central.core.logging import masker
from tests.test_api import Env

ADMIN_KEY = "dummy-admin-key-0000000000"


@pytest.fixture
async def env(home: Path):
    e = Env(home)
    yield e
    await e.http.aclose()
    await e.app.http.aclose()


def browser(env: Env) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=env.api), base_url="http://test/ui")


async def login(c: httpx.AsyncClient, key: str) -> dict:
    res = await c.post("/api/login", json={"key": key})
    assert res.status_code == 200, res.text
    return res.json()["data"]


async def data(c: httpx.AsyncClient, path: str, **params) -> dict:
    res = await c.get(path, params=params)
    body = res.json()
    assert body["ok"], body
    return body["data"]


async def trade(env: Env) -> tuple[int, str, int, str]:
    """서브계좌 둘. A 는 삼성전자 10주 매수 후 5주 매도, B 는 거부 1건."""
    env.fund("KRW", "10000000")
    await env.admin("PATCH", "/admin/broker-accounts/real", json={"trading_enabled": True})
    a_id, a_key = await env.new_sub("A", ["KR"], KRW="2000000")
    b_id, b_key = await env.new_sub("B", ["KR"], KRW="1000000")
    async with env.sdk(a_key) as cs:
        o = await cs.buy("KR:005930", 10, "61000")
        env.sim.fill(o["broker_order_no"], 10, D("61000"))
        await env.sim.drain()
        s = await cs.sell("KR:005930", 5, "62000")
        env.sim.fill(s["broker_order_no"], 5, D("62000"))
        await env.sim.drain()
    async with env.sdk(b_key) as cs:
        from central.broker.base import BrokerReject
        env.sim.reject_next = BrokerReject("40070000", "모의투자 주문처리가 안되었습니다(매매불가 종목)")
        await cs.buy("KR:005930", 1, "61000")
    return a_id, a_key, b_id, b_key


async def test_login_and_page(env: Env) -> None:
    async with browser(env) as c:
        page = await c.get("/")
        assert page.status_code == 200 and "중앙증권" in page.text
        assert (await c.get("/static/app.js")).status_code == 200
        assert (await c.get("/api/me")).status_code == 401
        assert (await c.post("/api/login", json={"key": "wrong-key"})).status_code == 401

        assert (await login(c, ADMIN_KEY))["role"] == "admin"
        cookie = next(iter(c.cookies.jar))
        assert cookie.name == ui.COOKIE and cookie.path == "/ui" and ADMIN_KEY not in cookie.value
        me = await data(c, "/api/me")
        assert me["role"] == "admin" and {w["type"] for w in me["widgets"]} == set(ui.WIDGETS)

        await c.post("/api/logout")
        assert (await c.get("/api/me")).status_code == 401
    # 관리자 API 의 헤더 인증은 그대로
    assert (await env.http.get("/admin/broker-accounts", headers={"X-Admin-Key": ADMIN_KEY})).status_code == 200


async def test_admin_widgets(env: Env) -> None:
    a_id, _, b_id, _ = await trade(env)
    async with browser(env) as c:
        await login(c, ADMIN_KEY)
        st = await data(c, "/api/w/status")
        assert {b["alias"] for b in st["brokers"]} == {"real", "paper"} and st["unknown_orders"] == 0
        assert "log" in st and "programs" in st

        acc = await data(c, "/api/w/accounts")
        rows = {r["id"]: r for r in acc["rows"]}
        assert rows[a_id]["day"] == {"orders": 2, "filled": 2, "rejected": 0, "unknown": 0, "working": 0, "reject_rate": 0.0}
        assert rows[b_id]["day"]["rejected"] == 1 and rows[b_id]["day"]["reject_rate"] == 1.0
        assert rows[a_id]["pnl"]["KRW"]["realized_pnl"] == "5000"
        assert any(r["type"] == "system" for r in acc["rows"])          # 관리자는 미배정도 본다

        blot = await data(c, "/api/w/blotter")
        assert len(blot["rows"]) == 3 and blot["counts"] == {"FILLED": 2, "REJECTED": 1}
        assert blot["rows"][0]["reason"].startswith("[40070000]")
        filled = [r for r in blot["rows"] if r["status"] == "FILLED"]
        assert all(r["accept_ms"] is not None for r in filled)
        assert len((await data(c, "/api/w/blotter", sub=str(b_id)))["rows"]) == 1
        assert len((await data(c, "/api/w/blotter", status="REJECTED"))["rows"]) == 1

        detail = await data(c, f"/api/orders/{filled[-1]['id']}")
        assert [e["status"] or e["type"] for e in detail["events"]][:2] == ["ACCEPTED", "FILL"]
        assert len(detail["fills"]) == 1 and detail["order"]["accept_ms"] is not None

        curve = await data(c, "/api/w/pnl_curve")
        [s] = [x for x in curve["series"] if x["sub_id"] == a_id]
        assert s["points"][-1][1] == "5000"
        daily = await data(c, "/api/w/pnl_daily", days="7")
        [s] = [x for x in daily["series"] if x["sub_id"] == a_id]
        assert s["total"]["wins"] == 1 and s["total"]["win_rate"] == 1.0 and len(s["days"]) == 1

        pos = await data(c, "/api/w/positions")
        assert [(p["sub_id"], p["qty"]) for p in pos["rows"]] == [(a_id, 5)]
        calls = await data(c, "/api/w/calls")
        assert {b["alias"] for b in calls["brokers"]} == {"real", "paper"}       # SimBroker 는 통계 없음
        jobs = await data(c, "/api/w/jobs")
        assert {j["name"] for j in jobs["jobs"]} >= {"maintenance", "kr_close"}
        assert "groups" in await data(c, "/api/w/errors")
        assert "supervisor" in await data(c, "/api/w/programs")


async def test_sub_scope(env: Env) -> None:
    a_id, a_key, b_id, _ = await trade(env)
    async with browser(env) as c:
        me = await login(c, a_key)
        assert me == {"role": "sub", "name": "A"}
        me = await data(c, "/api/me")
        assert [s["id"] for s in me["subs"]] == [a_id]
        assert {w["type"] for w in me["widgets"]} == {"status", "accounts", "pnl_curve", "pnl_daily", "blotter", "positions",
                                                    "chart", "symbols"}

        for w in ("calls", "errors", "jobs", "programs"):
            assert (await c.get(f"/api/w/{w}")).status_code == 404
        blot = await data(c, "/api/w/blotter", sub=str(b_id))            # 다른 서브계좌 지정은 무시
        assert {r["sub_id"] for r in blot["rows"]} == {a_id} and blot["counts"] == {"FILLED": 2}
        assert [r["id"] for r in (await data(c, "/api/w/accounts"))["rows"]] == [a_id]
        st = await data(c, "/api/w/status")
        assert [b["alias"] for b in st["brokers"]] == ["real"] and "log" not in st and "programs" not in st

        async with browser(env) as admin:
            await login(admin, ADMIN_KEY)
            b_order = (await data(admin, "/api/w/blotter", sub=str(b_id)))["rows"][0]["id"]
        assert (await c.get(f"/api/orders/{b_order}")).status_code == 404


async def test_layout_save_and_reset(env: Env) -> None:
    _, a_key, _, _ = await trade(env)
    async with browser(env) as c, browser(env) as other:
        await login(c, a_key)
        await login(other, ADMIN_KEY)
        d = await data(c, "/api/layout")
        assert not d["custom"] and [p["id"] for p in d["layout"]["pages"]] == ["overview", "blotter", "pnl", "app"]
        assert "system" in [p["id"] for p in (await data(other, "/api/layout"))["layout"]["pages"]]

        mine = {"pages": [{"id": "main", "title": "내 화면", "widgets": [
            {"type": "blotter", "w": 8, "params": {"status": "FILLED"}}, {"type": "positions", "w": 4, "title": "보유"}]}]}
        res = await c.put("/api/layout", json=mine)
        assert res.json()["ok"]
        d = await data(c, "/api/layout")
        assert d["custom"] and d["layout"]["pages"][0]["widgets"][1]["title"] == "보유"
        assert not (await data(other, "/api/layout"))["custom"]            # 사용자별로 따로

        bad = {"pages": [{"id": "x", "title": "x", "widgets": [{"type": "calls"}]}]}   # 서브계좌에 없는 위젯
        assert (await c.put("/api/layout", json=bad)).status_code == 400
        dup = {"pages": [{"id": "x", "title": "x", "widgets": []}, {"id": "x", "title": "y", "widgets": []}]}
        assert (await c.put("/api/layout", json=dup)).status_code == 400
        assert (await c.put("/api/layout", json={"pages": [{"id": "x", "title": "x", "widgets": [
            {"type": "blotter", "w": 13}]}]})).status_code == 400

        assert not (await c.delete("/api/layout")).json()["data"]["custom"]
        assert not (await data(c, "/api/layout"))["custom"]


def test_call_stats() -> None:
    now = [10_000.0]
    st = CallStats(clock=lambda: now[0])
    assert classify("/uapi/domestic-stock/v1/trading/order-cash", "POST") == "order"
    assert classify("/uapi/domestic-stock/v1/trading/inquire-balance", "GET") == "account"
    assert classify("/uapi/domestic-stock/v1/quotations/inquire-price", "GET") == "quote"
    assert classify("/uapi/domestic-stock/v1/ranking/fluctuation", "GET") == "ranking"
    st.record("order", "VTTC0012U", 900, 60, True, False)
    now[0] += 30
    st.record("quote", "FHKST01010100", 100, 40, False, True)
    snap = st.snapshot()
    assert snap["windows"]["1m"] == {"calls": 2, "errors": 1, "rate_limited": 1, "wait_avg_ms": 500.0,
                                     "wait_p95_ms": 900, "elapsed_avg_ms": 50.0}
    assert sum(snap["per_minute"]) == 2 and snap["per_minute_kind"]["order"][59] == 1
    now[0] += 3601
    assert st.snapshot()["windows"]["60m"]["calls"] == 0


def test_log_buffer_masks_and_groups() -> None:
    masker.add(["SECRETVALUE123"])
    try:
        buf = LogBuffer()
        lg = logging.getLogger("test.ui.buffer")
        lg.addHandler(buf)
        lg.warning("재연결 대기 1초 key SECRETVALUE123")
        lg.warning("재연결 대기 2초 key SECRETVALUE123")
        lg.info("무시")
        try:
            raise OSError("차단됨 appkey=abcdef")
        except OSError:
            lg.exception("수신 오류")
        lg.removeHandler(buf)
        assert len(buf.items) == 3 and all("SECRETVALUE123" not in i["message"] for i in buf.items)
        assert buf.items[2]["exc"] == "OSError: 차단됨 appkey=****"
        groups = buf.groups()
        assert groups[0]["count"] == 2 and groups[0]["message"].startswith("재연결 대기 2초")
    finally:
        masker.clear()


def _bars(n: int = 61) -> list:
    from datetime import datetime, timedelta

    from central.broker.base import Candle
    from central.core.market_hours import KST
    t0 = datetime(2026, 10, 14, 9, 0, tzinfo=KST)
    return [Candle(t0 + timedelta(minutes=i), D(61000 + i), D(61100 + i), D(60900 + i), D(61050 + i), 100 + i)
            for i in range(n)]


async def test_chart_and_symbols(env: Env) -> None:
    a_id, a_key, b_id, _ = await trade(env)
    for sim in (env.sim, env.paper):
        sim.candles[("KR:005930", "1m")] = _bars()
    async with browser(env) as c:
        await login(c, ADMIN_KEY)
        d = await data(c, "/api/w/chart")                                  # 종목 생략 → 최근 체결 종목
        assert d["symbol"] == "KR:005930" and d["error"] is None and d["symbols"][0]["fills"] == 2
        assert len(d["candles"]) == 61 and d["candles"][0][0].startswith("2026-10-14T09:00")
        assert [(m["side"], m["qty"]) for m in d["markers"]] == [("buy", 10), ("sell", 5)]
        assert [(p["sub"], p["qty"], D(p["avg_price"])) for p in d["positions"]] == [("A", 5, D("61000"))]
        calls = env.sim.candle_calls + env.paper.candle_calls
        await data(c, "/api/w/chart", symbol="KR:005930")                  # 짧은 캐시: KIS 를 다시 부르지 않는다
        assert env.sim.candle_calls + env.paper.candle_calls == calls
        five = await data(c, "/api/w/chart", symbol="KR:005930", interval="5m")
        assert len(five["candles"]) == 13
        assert (await c.get("/api/w/chart", params={"interval": "2m"})).status_code == 400
        assert (await data(c, "/api/w/chart", sub=str(b_id)))["symbol"] is None   # B 는 체결 없음

        sym = await data(c, "/api/w/symbols")
        [row] = [r for r in sym["rows"] if r["sub_id"] == a_id]
        assert (row["buy_qty"], row["sell_qty"], row["held"], row["pnl"], row["wins"]) == (10, 5, 5, "5000", 1)

    async with browser(env) as c:
        await login(c, a_key)
        d = await data(c, "/api/w/chart", sub=str(b_id))                  # 다른 서브계좌 지정은 무시
        assert {m["sub"] for m in d["markers"]} == {"A"}
        assert {r["sub_id"] for r in (await data(c, "/api/w/symbols"))["rows"]} == {a_id}
        layout = (await data(c, "/api/layout"))["layout"]
        app_page = next(p for p in layout["pages"] if p["id"] == "app")
        assert app_page["vars"] == ["date", "symbol"]
        page = {"pages": [{"id": "x", "title": "x", "vars": ["symbol"], "widgets": [{"type": "chart"}]}]}
        assert (await c.put("/api/layout", json=page)).json()["ok"]
        page["pages"][0]["vars"] = ["nope"]
        assert (await c.put("/api/layout", json=page)).status_code == 400
