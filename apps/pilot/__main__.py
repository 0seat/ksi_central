"""파일럿: 1주 무인 운영 시험용 가상 서브계좌 프로그램.

    python -m apps.pilot setup [--account paper] [--budget 5000000]   서브계좌/키 준비 (서버 실행 중)
    python -m apps.pilot run                                           실행 (보통 감시자가 띄운다)
    python -m apps.pilot report                                        오늘 보고서를 지금 만든다

설정: %CENTRAL_HOME%\\secrets\\apps\\pilot.toml (setup 이 만든다)
    base_url = "http://127.0.0.1:8700"
    api_key = "cs_..."              서브계좌 키 (관리자 키 아님)
    discord_webhook = "..."         비우면 파일럿 알림 없음 (기본: 중앙증권과 같은 웹훅)
상태와 보고서: %CENTRAL_HOME%\\apps\\pilot\\ (reports/YYYYMMDD.json, stats/)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tomllib
from decimal import Decimal
from pathlib import Path

import httpx

from central.client import CentralClient
from central.core.logging import masker, setup_logging
from central.ops.notify import Notifier
from central.settings import load_settings

SUB_NAME = "PILOT"
KEY_LABEL = "pilot"


def config_path(settings) -> Path:
    return settings.secrets_dir / "apps" / "pilot.toml"


def load_config(settings) -> dict:
    path = config_path(settings)
    if not path.is_file():
        raise SystemExit(f"{path} 없음: 먼저 python -m apps.pilot setup")
    with path.open("rb") as f:
        return tomllib.load(f)


# ───────────── setup ─────────────
async def _current_key(settings, base: str, sub_id: int) -> str | None:
    """설정 파일의 키가 아직 유효하고 같은 서브계좌 것이면 그대로 쓴다."""
    try:
        key = load_config(settings)["api_key"]
    except (SystemExit, KeyError, tomllib.TOMLDecodeError):
        return None
    async with CentralClient(base, key) as cs:
        try:
            return key if (await cs.me())["id"] == sub_id else None
        except Exception:
            return None



async def setup(args: argparse.Namespace) -> int:
    from apps.pilot.runner import SYMBOLS

    settings = load_settings()
    if settings.admin_key is None:
        print("관리자 키 없음: python -m central gen-admin-key", file=sys.stderr)
        return 2
    if settings.accounts[args.account].profile.env != "paper" and not args.confirm_real:
        print("파일럿은 모의 계좌용입니다. 실전에 쓰려면 --confirm-real (실제 주문이 나갑니다)", file=sys.stderr)
        return 2
    base = args.base_url or f"http://{settings.server.hosts[0]}:{settings.server.port}"
    async with httpx.AsyncClient(base_url=base + "/api/v1", timeout=30,
                                 headers={"X-Admin-Key": settings.admin_key.get_secret_value()}) as admin:
        async def call(method: str, path: str, **kw):
            body = (await admin.request(method, path, **kw)).json()
            if not body["ok"]:
                raise SystemExit(f"{method} {path}: {body['error']['code']} {body['error']['message']}")
            return body["data"]

        subs = {s["name"]: s for s in await call("GET", "/admin/sub-accounts", params={"broker_account_id": args.account})}
        sub = subs.get(SUB_NAME) or await call("POST", "/admin/sub-accounts", json={
            "broker_account_id": args.account, "name": SUB_NAME, "type": "app", "markets": ["KR"]})
        need = Decimal(args.budget) - Decimal(sub["balances"].get("KRW", {}).get("cash", "0"))
        if need > 0:
            await call("POST", "/admin/transfers", json={"from": subs["unallocated"]["id"], "to": sub["id"],
                                                         "currency": "KRW", "amount": str(need), "memo": "파일럿 예산"})
        # 시장가 매수는 상한가(+30%)로 홀드와 한도를 계산하므로 고가 종목(SK하이닉스 약 145만원)도 들어가게
        await call("PUT", f"/admin/sub-accounts/{sub['id']}/risk-limits", json={
            "market": "KR", "max_order_amount": "2500000", "max_open_orders": 4, "max_daily_loss": "300000",
            "allowed_symbols": list(SYMBOLS)})
        key = await _current_key(settings, base, sub["id"])
        if key is None or args.new_key:
            for k in await call("GET", "/admin/api-keys", params={"sub_account_id": sub["id"]}):
                if k.get("label") == KEY_LABEL and not k.get("revoked_at"):
                    await call("DELETE", f"/admin/api-keys/{k['id']}")
            key = (await call("POST", f"/admin/sub-accounts/{sub['id']}/api-keys", json={"label": KEY_LABEL}))["api_key"]
            print("API 키 발급 (실행 중인 파일럿은 재시작해야 새 키를 씀)")
        else:
            print("기존 API 키 유지")

    webhook = settings.discord_webhook.get_secret_value() if settings.discord_webhook and not args.no_discord else ""
    path = config_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# 파일럿 설정 (python -m apps.pilot setup 이 생성). 서브계좌 API 키가 들어 있으니 공유 금지\n"
        f'base_url = "{base}"\napi_key = "{key}"\ndiscord_webhook = "{webhook}"\n', encoding="utf-8")
    print(f"서브계좌 {SUB_NAME} #{sub['id']} ({args.account}) 예산 KRW {args.budget}, 한도 설정")
    print(f"설정 파일: {path}")
    if not settings.accounts[args.account].profile.trading_enabled:
        print(f"주의: central.toml [accounts.{args.account}] trading_enabled = false 라 주문이 거부됩니다")
    return 0


# ───────────── run / report ─────────────
def _make(settings, cfg: dict):
    from apps.pilot.runner import Pilot

    masker.add([cfg["api_key"], cfg.get("discord_webhook", "")])
    notifier = Notifier(cfg.get("discord_webhook") or None, source="파일럿",
                        min_level=settings.ops.notify_min_level,
                        cooldown_seconds=settings.ops.alert_cooldown_minutes * 60)
    cs = CentralClient(cfg["base_url"], cfg["api_key"], timeout=30)
    stop = os.environ.get("CENTRAL_STOP_FILE")
    pilot = Pilot(cs, notifier, settings.apps_dir / "pilot", stop_file=Path(stop) if stop else None)
    return pilot, cs, notifier


async def run(args: argparse.Namespace) -> int:
    settings = load_settings()
    setup_logging(settings, name="pilot", console=not os.environ.get("CENTRAL_SUPERVISED"))
    pilot, cs, notifier = _make(settings, load_config(settings))
    try:
        await pilot.run()
    finally:
        await cs.close()
        await asyncio.to_thread(notifier.close)
    return 0


async def report(args: argparse.Namespace) -> int:
    settings = load_settings()
    setup_logging(settings, name="pilot")
    pilot, cs, notifier = _make(settings, load_config(settings))
    try:
        await pilot.resync()
        r = await pilot.report()
        print(pilot.summary(r))
    finally:
        await cs.close()
        await asyncio.to_thread(notifier.close)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="apps.pilot", description="파일럿 서브계좌 프로그램")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup", help="서브계좌, 예산, 한도, API 키 준비 (서버 실행 중이어야 함)")
    s.add_argument("--account", default="paper")
    s.add_argument("--budget", default="5000000", help="KRW")
    s.add_argument("--base-url")
    s.add_argument("--no-discord", action="store_true", help="파일럿 알림을 보내지 않음")
    s.add_argument("--new-key", action="store_true", help="기존 키가 유효해도 새로 발급")
    s.add_argument("--confirm-real", action="store_true")
    s.set_defaults(func=setup)
    sub.add_parser("run", help="실행").set_defaults(func=run)
    sub.add_parser("report", help="오늘 보고서를 지금 만든다 (진행 중 데이터 기준)").set_defaults(func=report)
    args = p.parse_args()
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
