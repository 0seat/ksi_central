"""M4 검증: 실행 중인 API 서버에 외부 프로그램처럼 접속해 SDK 로 주문한다.

    python -m central serve                                       (다른 창에서 실행)
    python -m tools.api_trial --account paper                     모의 국내
    python -m tools.api_trial --account real --confirm-real       실전 국내 (실제 돈이 움직임)

흐름
 1. 관리자 API(관리자 키는 secrets.toml 에서 읽고 출력하지 않음)로 서브계좌 생성, 자금 배정, API 키 발급
 2. 실계좌 거래를 실행 중에만 켬
 3. SDK(서브계좌 키만 사용)로 WebSocket 구독, 지정가 접수 후 취소, 시장가 매수 후 매도
 4. 대사 후 거래 스위치를 원래대로 끄고 API 키 폐기
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from decimal import Decimal

import httpx

from central.client import CentralClient, CentralError
from central.settings import load_settings

SYMBOL = "KR:005930"


def step(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'OK' if ok else 'FAIL':4}] {name:<30} {detail}", flush=True)
    return ok


async def wait_status(cs: CentralClient, order_id: int, wanted: set[str], timeout: float = 30) -> str:
    deadline = time.monotonic() + timeout
    status = ""
    while time.monotonic() < deadline:
        status = (await cs.order(order_id))["status"]
        if status in wanted:
            return status
        await asyncio.sleep(0.5)
    return status


async def run(args: argparse.Namespace) -> int:
    settings = load_settings()
    if settings.admin_key is None:
        print("관리자 키 없음: python -m central gen-admin-key")
        return 2
    if settings.accounts[args.account].profile.env == "real" and not args.confirm_real:
        print("실전 계좌는 --confirm-real 이 필요합니다 (실제 주문이 나갑니다)")
        return 2
    base = args.base_url or f"http://{settings.server.hosts[0]}:{settings.server.port}"
    admin = httpx.AsyncClient(base_url=base + "/api/v1", timeout=30,
                              headers={"X-Admin-Key": settings.admin_key.get_secret_value()})
    results: list[bool] = []
    key_id = None
    print(f"\n=== API 검증 [{args.account}/KR] {base} ===")

    async def call(method: str, path: str, **kw):
        body = (await admin.request(method, path, **kw)).json()
        if not body["ok"]:
            raise CentralError(body["error"]["code"], body["error"]["message"], 0)
        return body["data"]

    try:
        health = await call("GET", f"/admin/broker-accounts/{args.account}/health")
        results.append(step("서버/체결통보 연결", health.get("notice_connected", health["connected"]), base))

        subs = {s["name"]: s for s in await call("GET", "/admin/sub-accounts", params={"broker_account_id": args.account})}
        sub = subs.get("SDK-TEST") or await call("POST", "/admin/sub-accounts", json={
            "broker_account_id": args.account, "name": "SDK-TEST", "markets": ["KR"]})
        un = subs["unallocated"]["id"]
        need = Decimal(args.budget) - Decimal(sub["balances"]["KRW"]["available"])
        if need > 0:
            await call("POST", "/admin/transfers", json={"from": un, "to": sub["id"], "currency": "KRW", "amount": str(need)})
        issued = await call("POST", f"/admin/sub-accounts/{sub['id']}/api-keys", json={"label": "api_trial"})
        key_id = issued["id"]
        results.append(step("서브계좌/키 준비", True, f"sub={sub['id']} 키 {issued['prefix']}..."))
        await call("PATCH", f"/admin/broker-accounts/{args.account}", json={"trading_enabled": True})

        events: list[dict] = []
        async with CentralClient(base, issued["api_key"], timeout=30) as cs:
            async def listen():
                async for msg in cs.stream():
                    events.append(msg)
            listener = asyncio.create_task(listen())
            await asyncio.sleep(1)

            me = await cs.me()
            results.append(step("SDK /me", me["id"] == sub["id"], f"{me['name']} trading={me['trading_enabled']}"))
            q = await cs.quote(SYMBOL)
            results.append(step("SDK 현재가", True, f"{q['last']} (하한 {q['lower_limit']})"))

            tag = time.strftime("%H%M%S")
            oa = await cs.buy(SYMBOL, 1, q["lower_limit"], client_order_id=f"api-a-{tag}")
            results.append(step("지정가 접수", oa["status"] == "ACCEPTED", f"{oa['price']} {oa['status']} {oa['reject_reason'] or ''}"))
            if oa["status"] == "ACCEPTED":
                await cs.cancel(oa["id"])
                results.append(step("취소", await wait_status(cs, oa["id"], {"CANCELED"}) == "CANCELED"))

            ob = await cs.buy(SYMBOL, 1, client_order_id=f"api-b1-{tag}")  # 시장가
            st = await wait_status(cs, ob["id"], {"FILLED", "REJECTED", "CANCELED"}, 60)
            results.append(step("시장가 매수 체결", st == "FILLED", st))
            if st == "FILLED":
                os_ = await cs.sell(SYMBOL, 1, client_order_id=f"api-b2-{tag}")
                st = await wait_status(cs, os_["id"], {"FILLED", "REJECTED", "CANCELED"}, 60)
                results.append(step("시장가 매도 체결", st == "FILLED", st))

            await asyncio.sleep(1)
            listener.cancel()
            chans = sorted({m["ch"] for m in events})
            fills = sum(1 for m in events if m["ch"] == "fills")
            results.append(step("WebSocket 이벤트", fills >= 2 or st != "FILLED", f"{len(events)}건 채널 {chans}, 체결 {fills}"))
            bal = await cs.balance()
            print(f"  잔고 KRW {bal['KRW']['cash']} (홀드 {bal['KRW']['held']}), 실현손익 {bal['KRW']['realized_pnl']}")

        rec = await call("POST", "/admin/reconcile", json={"broker_account_id": args.account, "market": "KR",
                                                          "halt_on_mismatch": False})
        qty_ok = not any("수량" in d for d in rec["diffs"])
        results.append(step("대사 (수량)", qty_ok, "; ".join(rec["diffs"]) or "일치"))
    except CentralError as e:
        results.append(step("오류", False, f"{e.code} {e.message}"))
    finally:
        try:
            await call("PATCH", f"/admin/broker-accounts/{args.account}", json={"trading_enabled": False})
            if key_id is not None:
                await call("DELETE", f"/admin/api-keys/{key_id}")
        finally:
            await admin.aclose()

    print(f"\n결과: {sum(results)}/{len(results)} 통과 (거래 스위치 끔, 검증용 키 폐기)")
    return 0 if all(results) else 1


def main() -> int:
    p = argparse.ArgumentParser(description="M4 API/SDK 실주문 검증 (국내 1주)")
    p.add_argument("--account", required=True)
    p.add_argument("--base-url")
    p.add_argument("--budget", default="400000", help="서브계좌에 맞춰 둘 KRW (시장가 홀드는 상한가 기준)")
    p.add_argument("--confirm-real", action="store_true")
    return asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
