"""M4: API 서버, API 키, SDK, WebSocket, 마이그레이션."""

from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from central.api.server import create_app
from central.app import CentralApp
from central.broker.sim import SimBroker
from central.client import CentralClient, CentralError
from central.settings import SettingsError, load_settings
from central.storage import migrate
from central.storage.db import Base, Database
from tests.conftest import CENTRAL_TOML, write_home
from tests.oms_env import KR_OPEN, Clock

ADMIN = {"X-Admin-Key": "dummy-admin-key-0000000000"}
BASE = "http://test/api/v1"


class Env:
    def __init__(self, home: Path) -> None:
        self.clock = Clock(KR_OPEN)
        self.sim = SimBroker("real", integrated_margin=True, clock=self.clock)
        self.sim.set_quote("KR:005930", D("61000"), upper=D("79300"), lower=D("42700"))
        self.sim.set_quote("US:AAPL", D("187.25"))
        self.paper = SimBroker("paper", clock=self.clock)
        self.app = CentralApp(load_settings(home), db=Database.memory(),
                              brokers={"real": self.sim, "paper": self.paper}, clock=self.clock)
        self.api = create_app(self.app, manage_lifecycle=False)
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.api), base_url=BASE)

    def fund(self, currency: str, amount: str) -> None:
        self.sim.deposit(currency, D(amount))
        self.app.accounts.deposit_unallocated("real", currency, D(amount))

    async def admin(self, method: str, path: str, **kw) -> dict:
        res = await self.http.request(method, path, headers=ADMIN, **kw)
        body = res.json()
        assert body["ok"], body
        return body["data"]

    async def new_sub(self, name: str, markets: list[str], **cash: str) -> tuple[int, str]:
        sub = await self.admin("POST", "/admin/sub-accounts", json={
            "broker_account_id": "real", "name": name, "markets": markets})
        un = self.app.accounts.unallocated("real").id
        for cur, amount in cash.items():
            await self.admin("POST", "/admin/transfers", json={"from": un, "to": sub["id"], "currency": cur, "amount": amount})
        key = await self.admin("POST", f"/admin/sub-accounts/{sub['id']}/api-keys", json={"label": name})
        return sub["id"], key["api_key"]

    def sdk(self, key: str) -> CentralClient:
        return CentralClient("http://test", key, transport=httpx.ASGITransport(app=self.api))


@pytest.fixture
async def env(home: Path):
    e = Env(home)
    yield e
    await e.http.aclose()
    await e.app.http.aclose()


# ───────────── 마이그레이션 ─────────────
def test_migrations_match_models(tmp_path: Path) -> None:
    db = Database.file(tmp_path / "m.db")
    db.migrate()
    assert migrate.current(db.engine) == migrate.head()
    with db.engine.connect() as conn:
        diffs = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diffs == []


def test_legacy_create_all_db_is_stamped_and_upgraded(tmp_path: Path) -> None:
    db = Database.file(tmp_path / "legacy.db")
    legacy = [t for name, t in Base.metadata.tables.items()
              if name not in ("api_keys", "instruments", "candles", "candle_coverage", "market_days", "halts",
                              "job_runs")]
    Base.metadata.create_all(db.engine, tables=legacy)  # M3 까지의 DB
    db.migrate()
    tables = set(inspect(db.engine).get_table_names())
    assert {"api_keys", "instruments", "candles", "orders", "halts", "job_runs"} <= tables
    assert migrate.current(db.engine) == migrate.head()


# ───────────── 설정 ─────────────
@pytest.mark.parametrize("host,allowed", [
    ("127.0.0.1", True), ("100.101.102.103", True), ("fd7a:115c:a1e0::1", True),
    ("192.168.0.10", False), ("0.0.0.0", False), ("localhost", False),
])
def test_bind_hosts_loopback_or_tailscale(tmp_path: Path, host: str, allowed: bool) -> None:
    write_home(tmp_path, central=CENTRAL_TOML.replace('hosts = ["127.0.0.1"]', f'hosts = ["{host}"]'))
    if allowed:
        assert load_settings(tmp_path).server.hosts == [host]
    else:
        with pytest.raises(SettingsError, match="hosts"):
            load_settings(tmp_path)


# ───────────── 인증 ─────────────
async def test_admin_and_sub_auth(env: Env) -> None:
    assert (await env.http.get("/admin/broker-accounts")).status_code == 401
    assert (await env.http.get("/admin/broker-accounts", headers={"X-Admin-Key": "wrong"})).status_code == 401
    assert (await env.http.get("/me")).status_code == 401
    assert (await env.http.get("/me", headers={"X-API-Key": "cs_nope"})).status_code == 401
    assert (await env.http.get("/health")).json()["data"]["status"] == "up"

    sub_id, key = await env.new_sub("A", ["KR"])
    me = (await env.http.get("/me", headers={"X-API-Key": key})).json()["data"]
    assert me["id"] == sub_id and me["markets"] == ["KR"] and me["trading_enabled"] is False

    keys = await env.admin("GET", "/admin/api-keys", params={"sub_account_id": sub_id})
    assert len(keys) == 1 and "api_key" not in keys[0] and key.startswith(keys[0]["prefix"])
    await env.admin("DELETE", f"/admin/api-keys/{keys[0]['id']}")
    assert (await env.http.get("/me", headers={"X-API-Key": key})).status_code == 401


async def test_api_key_is_stored_as_hash_only(env: Env) -> None:
    _, key = await env.new_sub("A", ["KR"])
    with env.app.db.engine.connect() as conn:
        dump = "\n".join(str(r) for r in conn.exec_driver_sql("SELECT * FROM api_keys"))
    assert key not in dump and key[9:] not in dump


async def test_unallocated_gets_no_api_key(env: Env) -> None:
    un = env.app.accounts.unallocated("real").id
    res = await env.http.post(f"/admin/sub-accounts/{un}/api-keys", headers=ADMIN)
    assert res.status_code == 400 and res.json()["error"]["code"] == "INVALID_REQUEST"


# ───────────── SDK 주문 흐름 ─────────────
async def test_sdk_order_flow_kr(env: Env) -> None:
    env.fund("KRW", "10000000")
    sub_id, key = await env.new_sub("krA", ["KR"], KRW="1000000")

    async with env.sdk(key) as cs:
        with pytest.raises(CentralError) as e:
            await cs.buy("KR:005930", qty=10, price="61000")
        assert e.value.code == "ACCOUNT_TRADING_DISABLED"

        await env.admin("PATCH", "/admin/broker-accounts/real", json={"trading_enabled": True})
        order = await cs.buy("KR:005930", qty=10, price="61000", client_order_id="krA-0001")
        assert order["status"] == "ACCEPTED" and order["route"] == "SOR" and order["price"] == "61000"
        again = await cs.buy("KR:005930", qty=10, price="61000", client_order_id="krA-0001")
        assert again["id"] == order["id"]  # 멱등성

        bal = await cs.balance()
        assert bal["KRW"]["held"] == "610000" and bal["KRW"]["available"] == "390000"

        env.sim.fill(order["broker_order_no"], 10, D("61000"))
        await env.sim.drain()
        detail = await cs.order(order["id"])
        assert detail["status"] == "FILLED" and len(detail["fills"]) == 1
        assert await cs.positions("KR") == [{"symbol": "KR:005930", "currency": "KRW", "qty": 10, "sellable": 10,
                                             "avg_price": "61000.0000", "realized_pnl": "0"}]

        sell = await cs.sell("KR:005930", qty=10, price="62000")
        env.sim.fill(sell["broker_order_no"], 10, D("62000"))
        await env.sim.drain()
        pnl = await cs.pnl(market="KR")
        assert pnl["KRW"]["realized_pnl"] == "10000" and pnl["KRW"]["fills"] == 2
        assert (await cs.balance())["KRW"]["cash"] == "1010000"
        types = [x["type"] for x in await cs.ledger(currency="KRW")]
        assert types == ["SELL", "BUY", "TRANSFER_IN"]
        assert len(await cs.fills(symbol="KR:005930")) == 2
        assert [o["status"] for o in await cs.orders(status="FILLED")] == ["FILLED", "FILLED"]


async def test_sdk_amend_cancel_and_cancel_all(env: Env) -> None:
    env.fund("KRW", "10000000")
    await env.admin("PATCH", "/admin/broker-accounts/real", json={"trading_enabled": True})
    _, key = await env.new_sub("krA", ["KR"], KRW="2000000")
    async with env.sdk(key) as cs:
        o1 = await cs.buy("KR:005930", 5, "60000")
        o2 = await cs.amend(o1["id"], "60500")
        assert o2["client_order_id"].endswith("#A1") and (await cs.order(o1["id"]))["status"] == "REPLACED"
        await cs.cancel(o2["id"])
        await env.sim.drain()
        assert (await cs.order(o2["id"]))["status"] == "CANCELED"

        await cs.buy("KR:005930", 1, "59000")
        await cs.buy("KR:005930", 1, "58000")
        result = await cs.cancel_all("KR")
        assert len(result["requested"]) == 2 and result["failed"] == []
        await env.sim.drain()
        assert (await cs.balance())["KRW"]["held"] == "0"


async def test_us_symbol_requires_registration(env: Env) -> None:
    env.fund("USD", "10000")
    await env.admin("PATCH", "/admin/broker-accounts/real", json={"trading_enabled": True})
    _, key = await env.new_sub("usB", ["US"], USD="1000")
    env.clock.t = env.clock.t.replace(hour=23)  # 미국장
    async with env.sdk(key) as cs:
        with pytest.raises(CentralError) as e:
            await cs.buy("US:AAPL", 3, "187.25")
        assert e.value.code == "INVALID_SYMBOL"
        await env.admin("PUT", "/admin/instruments", json={"symbol": "US:AAPL", "exchange": "NASD", "name": "Apple"})
        order = await cs.buy("US:AAPL", 3, "187.25")
        assert order["status"] == "ACCEPTED"
        assert [s["id"] for s in (await env.http.get("/market/symbols", params={"q": "app"}, headers={"X-API-Key": key})).json()["data"]] == ["US:AAPL"]


async def test_float_price_rejected(env: Env) -> None:
    _, key = await env.new_sub("A", ["KR"])
    res = await env.http.post("/me/orders", headers={"X-API-Key": key}, json={
        "client_order_id": "x", "symbol": "KR:005930", "side": "buy", "qty": 1, "price": 61000.5})
    assert res.status_code == 400 and res.json()["error"]["code"] == "INVALID_REQUEST"


async def test_sub_accounts_are_isolated(env: Env) -> None:
    env.fund("KRW", "10000000")
    await env.admin("PATCH", "/admin/broker-accounts/real", json={"trading_enabled": True})
    _, key_a = await env.new_sub("A", ["KR"], KRW="1000000")
    _, key_b = await env.new_sub("B", ["KR"], KRW="1000000")
    async with env.sdk(key_a) as a, env.sdk(key_b) as b:
        order = await a.buy("KR:005930", 1, "61000")
        with pytest.raises(CentralError) as e:
            await b.order(order["id"])
        assert e.value.code == "ORDER_NOT_FOUND" and e.value.status == 404
        with pytest.raises(CentralError):
            await b.cancel(order["id"])
        assert await b.orders() == []
        assert (await b.balance())["KRW"]["held"] == "0"


async def test_kill_switch_halts_sub_and_cancels(env: Env) -> None:
    env.fund("KRW", "10000000")
    await env.admin("PATCH", "/admin/broker-accounts/real", json={"trading_enabled": True})
    sub_id, key = await env.new_sub("A", ["KR"], KRW="1000000")
    async with env.sdk(key) as cs:
        await cs.buy("KR:005930", 1, "60000")
        res = await env.admin("POST", "/admin/kill-switch", json={
            "scope": "sub", "id": str(sub_id), "action": "halt", "cancel_open_orders": True})
        assert res["halted"] == [{"scope": "sub", "id": str(sub_id)}] and len(res["canceled"]["requested"]) == 1
        with pytest.raises(CentralError) as e:
            await cs.buy("KR:005930", 1, "60000")
        assert e.value.code == "HALTED"
        assert (await cs.me())["halted"] == {"KR": True}
        await env.admin("POST", "/admin/kill-switch", json={"scope": "sub", "id": str(sub_id), "action": "resume"})
        assert (await cs.buy("KR:005930", 1, "60000"))["status"] == "ACCEPTED"


async def test_admin_reconcile_and_fee_schedule(env: Env) -> None:
    env.fund("KRW", "1000000")
    result = await env.admin("POST", "/admin/reconcile", json={"broker_account_id": "real", "market": "KR"})
    assert result["ok"] is True
    assert len(await env.admin("GET", "/admin/reconciliations")) == 1

    fees = await env.admin("PUT", "/admin/fee-schedules", json={
        "broker_account_id": "real", "market": "KR", "side": "sell", "fee_rate": "0.00015", "tax_rate": "0.002"})
    assert fees[0]["fee_rate"] == "0.00015" and fees[0]["min_fee"] == "0"
    res = await env.http.put("/admin/fee-schedules", headers=ADMIN, json={
        "broker_account_id": "real", "market": "KR", "side": "sell", "fee_rate": 0.0001})
    assert res.status_code == 400


async def test_market_endpoints(env: Env) -> None:
    _, key = await env.new_sub("A", ["KR"])
    h = {"X-API-Key": key}
    q = (await env.http.get("/market/quote/KR:005930", headers=h)).json()["data"]
    assert q["last"] == "61000" and q["upper_limit"] == "79300"
    st = (await env.http.get("/market/status", headers=h)).json()["data"]["markets"]
    assert st["KR"]["accepting_orders"] is True and st["US"]["accepting_orders"] is False
    assert (await env.http.get("/market/fx", headers=ADMIN)).json()["data"]["rate"] == "1400"
    cal = (await env.http.get("/market/calendar", headers=h)).json()["data"]
    assert cal["market"] == "KR" and len(cal["days"]) == 31
    assert (await env.http.get("/market/status")).status_code == 401


# ───────────── WebSocket ─────────────
def test_websocket_delivers_only_own_events(home: Path) -> None:
    env = Env(home)
    env.fund("KRW", "10000000")
    with TestClient(env.api, base_url="http://test") as tc:
        def admin(method: str, path: str, **kw):
            body = tc.request(method, "/api/v1" + path, headers=ADMIN, **kw).json()
            assert body["ok"], body
            return body["data"]

        admin("PATCH", "/admin/broker-accounts/real", json={"trading_enabled": True})
        un = env.app.accounts.unallocated("real").id
        subs = {}
        for name in ("A", "B"):
            sid = admin("POST", "/admin/sub-accounts", json={"broker_account_id": "real", "name": name, "markets": ["KR"]})["id"]
            subs[name] = (sid, admin("POST", f"/admin/sub-accounts/{sid}/api-keys")["api_key"])

        with pytest.raises(Exception):
            with tc.websocket_connect("/api/v1/ws") as ws:
                ws.receive_json()

        with tc.websocket_connect("/api/v1/ws", headers={"X-API-Key": subs["A"][1]}) as ws:
            assert ws.receive_json()["data"]["type"] == "WELCOME"
            admin("POST", "/admin/transfers", json={"from": un, "to": subs["B"][0], "currency": "KRW", "amount": "1000"})
            admin("POST", "/admin/transfers", json={"from": un, "to": subs["A"][0], "currency": "KRW", "amount": "100000"})
            msg = ws.receive_json()
            assert msg["ch"] == "account" and msg["data"]["to"] == subs["A"][0]  # B 이체는 오지 않음

            res = tc.post("/api/v1/me/orders", headers={"X-API-Key": subs["A"][1]}, json={
                "client_order_id": "w1", "symbol": "KR:005930", "side": "buy", "qty": 1, "price": "61000"})
            assert res.json()["ok"]
            msg = ws.receive_json()
            assert msg["ch"] == "orders" and msg["data"]["status"] == "ACCEPTED"

            ws.send_json({"op": "subscribe", "channels": ["tick:KR:005930"]})
            assert ws.receive_json()["data"]["code"] == "MARKET_NOT_SUPPORTED"  # 이 환경엔 실시간 시세 계좌 없음
            assert ws.receive_json()["data"]["type"] == "SUBSCRIBED"
            admin("POST", "/admin/kill-switch", json={"scope": "market", "id": "KR", "action": "halt"})
            msg = ws.receive_json()
            assert msg["ch"] == "system" and msg["data"]["type"] == "HALT"


# ───────────── CLI ─────────────
def test_gen_admin_key_writes_without_printing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    from central.__main__ import main

    home = write_home(tmp_path / "h", secrets='[central]\nadmin_key = ""\n\n[accounts.paper]\napp_key = "a"\n')
    monkeypatch.setenv("CENTRAL_HOME", str(home))
    assert main(["gen-admin-key"]) == 0
    text = (home / "secrets" / "secrets.toml").read_text(encoding="utf-8")
    key = text.split('admin_key = "')[1].split('"')[0]
    assert key.startswith("csadm_") and len(key) > 40
    assert key not in capsys.readouterr().out
    assert main(["gen-admin-key"]) == 1  # 이미 있으면 --force 없이는 교체하지 않음
