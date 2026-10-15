"""python -m central sub ... : 서브계좌 준비 (실행 중인 서버의 관리자 API 를 부른다).

  sub list [--account paper]
  sub create MYBOT --account paper --markets KR --krw 3000000 --key-out C:/bots/mybot/central_key.toml \
      [--max-order-amount 1000000 --max-open-orders 4 --max-daily-loss 100000 --symbols KR:005930 KR:000660]
  sub fund MYBOT --krw 1000000          미배정 → MYBOT (음수면 MYBOT → 미배정)
  sub key MYBOT --key-out <경로> [--revoke-old]   새 API 키 발급
  sub limits MYBOT --market KR --max-order-amount 1000000 ...

키 파일(toml: base_url, sub_account_id, api_key)은 프로그램 쪽에서 SDK CentralClient.from_file(경로)로 읽는다.
키는 화면에 출력하지 않고 파일에만 쓴다. 저장소 폴더 안에는 쓰지 않는다 (public 저장소에 올라가지 않게).
기본 위치는 %CENTRAL_HOME%\\secrets\\apps\\<이름>.toml.
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

import httpx

from central.settings import REPO_ROOT, Settings, load_settings


class _Admin:
    def __init__(self, settings: Settings, base_url: str | None) -> None:
        if settings.admin_key is None:
            raise SystemExit("관리자 키 없음: python -m central gen-admin-key")
        host = settings.server.hosts[0]
        self.base = base_url or f"http://{f'[{host}]' if ':' in host else host}:{settings.server.port}"
        self.http = httpx.Client(base_url=self.base + "/api/v1", timeout=30,
                                 headers={"X-Admin-Key": settings.admin_key.get_secret_value()})

    def __call__(self, method: str, path: str, **kw):
        try:
            res = self.http.request(method, path, **kw)
        except httpx.ConnectError:
            raise SystemExit(f"서버에 연결할 수 없음 ({self.base}). 서버가 떠 있어야 한다") from None
        body = res.json()
        if not body["ok"]:
            raise SystemExit(f"{method} {path}: {body['error']['code']} {body['error']['message']}")
        return body["data"]

    def subs(self, account: str | None) -> dict[str, dict]:
        params = {"broker_account_id": account} if account else {}
        return {(s["broker_account_id"], s["name"]): s for s in self("GET", "/admin/sub-accounts", params=params)}

    def find(self, account: str, name: str) -> dict:
        sub = self.subs(account).get((account, name))
        if sub is None:
            raise SystemExit(f"{account} 에 서브계좌 {name} 없음")
        return sub


def key_path(settings: Settings, name: str, key_out: str | None) -> Path:
    path = Path(key_out).expanduser().resolve() if key_out else settings.secrets_dir / "apps" / f"{name.lower()}.toml"
    if path.is_relative_to(REPO_ROOT):
        raise SystemExit(f"키 파일을 저장소 안({REPO_ROOT})에 쓰지 않음: {path}")
    return path


def write_key(path: Path, base_url: str, sub: dict, key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        f"# 중앙증권 서브계좌 {sub['name']} (id {sub['id']}, {sub['broker_account_id']}) API 키. 비밀: 공유하거나 저장소에 넣지 말 것\n"
        f'base_url = "{base_url}"\nsub_account_id = {sub["id"]}\napi_key = "{key}"\n', encoding="utf-8")
    tmp.replace(path)


def _limits_body(args: argparse.Namespace, market: str) -> dict | None:
    body = {"market": market}
    for f in ("max_order_amount", "max_position_amount", "max_daily_loss"):
        if getattr(args, f) is not None:
            body[f] = str(getattr(args, f))
    if args.max_open_orders is not None:
        body["max_open_orders"] = args.max_open_orders
    if args.symbols:
        body["allowed_symbols"] = [s for s in args.symbols if s.upper().startswith(f"{market}:")]
    return body if len(body) > 1 else None


def _fund(admin: _Admin, account: str, sub: dict, amounts: dict[str, Decimal | None]) -> None:
    un = admin.find(account, "unallocated")
    for cur, amount in amounts.items():
        if not amount:
            continue
        src, dst = (un, sub) if amount > 0 else (sub, un)
        admin("POST", "/admin/transfers", json={"from": src["id"], "to": dst["id"], "currency": cur,
                                                "amount": str(abs(amount)), "memo": "sub cli"})
        print(f"{cur} {abs(amount)} {src['name']} → {dst['name']}")


def _issue(admin: _Admin, settings: Settings, sub: dict, key_out: str | None, revoke_old: bool) -> Path:
    path = key_path(settings, sub["name"], key_out)
    if revoke_old:
        for k in admin("GET", "/admin/api-keys", params={"sub_account_id": sub["id"]}):
            if not k.get("revoked_at"):
                admin("DELETE", f"/admin/api-keys/{k['id']}")
    key = admin("POST", f"/admin/sub-accounts/{sub['id']}/api-keys", json={"label": "sub-cli"})["api_key"]
    write_key(path, admin.base, sub, key)
    return path


def _show(admin: _Admin, account: str | None) -> None:
    for (acc, name), s in sorted(admin.subs(account).items()):
        cash = ", ".join(f"{c} {b['available']}/{b['cash']}" for c, b in s["balances"].items()) or "현금 없음"
        print(f"{s['id']:>4} {acc:<8} {name:<16} {s['type']:<9} {s['status']:<9} {','.join(s['markets']):<6} "
              f"주문가능/현금 {cash}")


def run(args: argparse.Namespace, settings: Settings | None = None, admin: _Admin | None = None) -> int:
    settings = settings or load_settings()
    admin = admin or _Admin(settings, args.base_url)
    if args.action == "list":
        _show(admin, args.account)
        return 0
    account = args.account
    if account not in settings.accounts:
        raise SystemExit(f"실계좌 {account} 없음 (설정: {', '.join(settings.accounts)})")
    if args.action == "create":
        if (account, args.name) in admin.subs(account):
            raise SystemExit(f"{account} 에 {args.name} 이 이미 있음. 돈은 sub fund, 키는 sub key")
        key_path(settings, args.name, args.key_out)   # 만들기 전에 경로부터 확인
        markets = [m.upper() for m in args.markets]
        sub = admin("POST", "/admin/sub-accounts", json={"broker_account_id": account, "name": args.name,
                                                         "type": args.type, "markets": markets,
                                                         "fx_mode": args.fx_mode})
        print(f"서브계좌 {sub['name']} id {sub['id']} ({account}, {','.join(markets)})")
        _fund(admin, account, sub, {"KRW": args.krw, "USD": args.usd})
        for m in markets:
            body = _limits_body(args, m)
            if body:
                admin("PUT", f"/admin/sub-accounts/{sub['id']}/risk-limits", json=body)
                print(f"한도 {m}: {body}")
        path = _issue(admin, settings, sub, args.key_out, False)
        print(f"API 키를 {path} 에 저장 (화면에는 출력하지 않음). 프로그램에서 CentralClient.from_file(경로)")
        return 0
    sub = admin.find(account, args.name)
    if args.action == "fund":
        _fund(admin, account, sub, {"KRW": args.krw, "USD": args.usd})
    elif args.action == "key":
        path = _issue(admin, settings, sub, args.key_out, args.revoke_old)
        print(f"새 API 키를 {path} 에 저장" + (" (이전 키 폐기)" if args.revoke_old else ""))
    elif args.action == "limits":
        body = _limits_body(args, args.market.upper())
        if body is None:
            raise SystemExit("바꿀 한도가 없음")
        admin("PUT", f"/admin/sub-accounts/{sub['id']}/risk-limits", json=body)
        print(f"한도 {args.market}: {body}")
    _show(admin, account)
    return 0


def _limit_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--max-order-amount", type=Decimal, help="1회 주문 금액 한도 (시장 통화)")
    p.add_argument("--max-position-amount", type=Decimal, help="종목당 보유 금액 한도")
    p.add_argument("--max-daily-loss", type=Decimal, help="일 손실 한도")
    p.add_argument("--max-open-orders", type=int, help="미체결 주문 수 한도")
    p.add_argument("--symbols", nargs="*", help="허용 종목 (예: KR:005930 US:AAPL)")


def add_parser(sub) -> None:
    s = sub.add_parser("sub", help="서브계좌 준비 (만들기, 자금, 한도, API 키 파일)")
    s.add_argument("--base-url", help="서버 주소 (기본: 설정의 첫 host)")
    ss = s.add_subparsers(dest="action", required=True)
    ls = ss.add_parser("list", help="서브계좌 목록과 잔고")
    ls.add_argument("--account", help="실계좌 (예: paper, real)")
    c = ss.add_parser("create", help="만들고 자금 배정, 한도, API 키 파일까지")
    c.add_argument("name")
    c.add_argument("--account", required=True, help="실계좌 (예: paper, real)")
    c.add_argument("--markets", nargs="+", default=["KR"], help="KR US")
    c.add_argument("--type", choices=["app", "strategy"], default="app")
    c.add_argument("--fx-mode", choices=["none", "broker"], default="none", help="broker: 통합증거금으로 원화로 미국 주문")
    c.add_argument("--krw", type=Decimal, help="미배정에서 옮길 원화")
    c.add_argument("--usd", type=Decimal, help="미배정에서 옮길 달러")
    c.add_argument("--key-out", help="키 파일 경로 (기본 %%CENTRAL_HOME%%\\secrets\\apps\\<이름>.toml)")
    _limit_args(c)
    f = ss.add_parser("fund", help="미배정과 자금 이동 (음수면 돌려받음)")
    f.add_argument("name")
    f.add_argument("--account", required=True)
    f.add_argument("--krw", type=Decimal)
    f.add_argument("--usd", type=Decimal)
    k = ss.add_parser("key", help="새 API 키 발급해 파일로")
    k.add_argument("name")
    k.add_argument("--account", required=True)
    k.add_argument("--key-out")
    k.add_argument("--revoke-old", action="store_true", help="기존 키 모두 폐기")
    lm = ss.add_parser("limits", help="한도 변경")
    lm.add_argument("name")
    lm.add_argument("--account", required=True)
    lm.add_argument("--market", required=True, choices=["KR", "US", "kr", "us"])
    _limit_args(lm)
    s.set_defaults(func=lambda a: _main(a))


def _main(args: argparse.Namespace) -> int:
    try:
        return run(args)
    except SystemExit as e:
        if isinstance(e.code, str):
            print(e.code, file=sys.stderr)
            return 2
        raise
