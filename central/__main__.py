"""`python -m central <command>`

    config            설정 요약 (비밀정보 제외)
    serve             API 서버 실행
    db upgrade        운영 DB 를 최신 스키마로
    db current        현재 스키마 리비전
    master            KIS 종목 마스터 적재 (국내 코스피/코스닥, 미국 나스닥/뉴욕/아멕스)
    gen-admin-key     관리자 키를 생성해 secrets.toml 에 기록 (화면에 출력하지 않음)
"""

from __future__ import annotations

import argparse
import re
import secrets
import sys

from central.settings import SettingsError, load_settings, resolve_home


def _cmd_config(_: argparse.Namespace) -> int:
    """설정을 읽어 비밀정보 없이 요약 출력."""
    settings = load_settings()
    print(f"CENTRAL_HOME : {settings.home}")
    print(f"server       : {settings.server.hosts} : {settings.server.port}")
    print(f"admin_key    : {'설정됨' if settings.admin_key else '없음'}")
    print(f"telegram     : {'설정됨' if settings.telegram_bot_token else '없음'}")
    for alias, acc in settings.accounts.items():
        p = acc.profile
        print(
            f"account[{alias}] env={p.env} markets={p.markets} routes={p.kr_routes} "
            f"default={p.kr_default_route} integrated_margin={p.integrated_margin} "
            f"rate={p.rate_limit_per_sec}/s trading_enabled={p.trading_enabled}"
        )
    return 0


def _cmd_serve(_: argparse.Namespace) -> int:
    from central.api.server import serve
    from central.app import CentralApp
    from central.core.logging import setup_logging

    settings = load_settings()
    setup_logging(settings)
    if settings.admin_key is None:
        print("[경고] 관리자 키가 없어 관리자 API 가 비활성입니다 (python -m central gen-admin-key)", file=sys.stderr)
    serve(CentralApp(settings))
    return 0


def _cmd_db(args: argparse.Namespace) -> int:
    from central.storage import migrate
    from central.storage.db import Database

    settings = load_settings()
    db = Database.file(settings.data_dir / "central.db")
    if args.action == "upgrade":
        migrate.upgrade(db.engine)
    print(f"스키마 리비전: {migrate.current(db.engine) or '(없음)'}")
    return 0


def _cmd_master(_: argparse.Namespace) -> int:
    """종목 마스터를 받아 운영 DB 에 적재 (서버를 띄우지 않고)."""
    import asyncio

    from central.marketdata.master import fetch_master
    from central.oms.catalog import InstrumentCatalog
    from central.core.instruments import InstrumentRegistry
    from central.storage.db import Database
    import httpx

    settings = load_settings()
    db = Database.file(settings.data_dir / "central.db")
    db.migrate()

    async def run() -> list:
        async with httpx.AsyncClient(timeout=60) as http:
            return await fetch_master(http)

    catalog = InstrumentCatalog(db, InstrumentRegistry())
    n = catalog.bulk_load(asyncio.run(run()))
    print(f"종목 마스터 {n}건 적재: {catalog.counts()}")
    return 0


def _cmd_gen_admin_key(args: argparse.Namespace) -> int:
    path = resolve_home() / "secrets" / "secrets.toml"
    if not path.is_file():
        print(f"secrets.toml 없음: {path}", file=sys.stderr)
        return 2
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(r'^(admin_key\s*=\s*)"([^"]*)"', re.MULTILINE)
    m = pattern.search(text)
    if m is None:
        print('secrets.toml 의 [central] 섹션에 admin_key = "" 줄이 없습니다', file=sys.stderr)
        return 2
    if m.group(2) and not args.force:
        print("admin_key 가 이미 있습니다. 교체하려면 --force", file=sys.stderr)
        return 1
    key = "csadm_" + secrets.token_urlsafe(32)
    path.write_text(pattern.sub(lambda mm: f'{mm.group(1)}"{key}"', text, count=1), encoding="utf-8")
    print(f"관리자 키를 기록했습니다: {path} ([central] admin_key). 값은 그 파일에서 확인하세요.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="central", description="중앙증권")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("config", help="설정 확인 (비밀정보는 출력하지 않음)").set_defaults(func=_cmd_config)
    sub.add_parser("serve", help="API 서버 실행").set_defaults(func=_cmd_serve)
    db = sub.add_parser("db", help="DB 스키마 관리")
    db.add_argument("action", choices=["upgrade", "current"])
    db.set_defaults(func=_cmd_db)
    sub.add_parser("master", help="KIS 종목 마스터 적재").set_defaults(func=_cmd_master)
    gk = sub.add_parser("gen-admin-key", help="관리자 키 생성 후 secrets.toml 에 기록")
    gk.add_argument("--force", action="store_true", help="기존 키 교체")
    gk.set_defaults(func=_cmd_gen_admin_key)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SettingsError as e:
        print(f"[설정 오류] {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
