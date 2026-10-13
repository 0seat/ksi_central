"""`python -m central <command>`"""

from __future__ import annotations

import argparse
import sys

from central.settings import SettingsError, load_settings


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="central", description="중앙증권")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("config", help="설정 확인 (비밀정보는 출력하지 않음)").set_defaults(func=_cmd_config)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SettingsError as e:
        print(f"[설정 오류] {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
