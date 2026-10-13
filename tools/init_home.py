"""CENTRAL_HOME 디렉토리 초기화.

    python -m tools.init_home [--home PATH] [--no-acl]

- 디렉토리 구조 생성 (config, secrets, state/tokens, data, logs, backups)
- 예시 설정 파일을 복사 (이미 있으면 건드리지 않음)
- Windows: secrets/, state/ 의 NTFS 권한을 현재 사용자 + SYSTEM + Administrators 로 제한
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from central.settings import REPO_ROOT, SettingsError, resolve_home

SUBDIRS = ["config", "secrets", "state/tokens", "data", "logs", "backups"]
RESTRICTED_DIRS = ["secrets", "state"]
TEMPLATES = {
    "config/central.toml": REPO_ROOT / "config" / "central.example.toml",
    "secrets/secrets.toml": REPO_ROOT / "config" / "secrets.example.toml",
}
# 언어 설정과 무관하게 동작하도록 잘 알려진 SID 사용
SID_SYSTEM = "*S-1-5-18"
SID_ADMINISTRATORS = "*S-1-5-32-544"


def restrict_acl(path: Path) -> None:
    user = f"{os.environ['USERDOMAIN']}\\{os.environ['USERNAME']}"
    cmd = [
        "icacls", str(path), "/inheritance:r",
        "/grant:r", f"{user}:(OI)(CI)F",
        "/grant:r", f"{SID_SYSTEM}:(OI)(CI)F",
        "/grant:r", f"{SID_ADMINISTRATORS}:(OI)(CI)F",
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CENTRAL_HOME 초기화")
    parser.add_argument("--home", help="기본값: 환경변수 CENTRAL_HOME 또는 C:\\ProgramData\\CentralSecurities")
    parser.add_argument("--no-acl", action="store_true", help="NTFS 권한 제한 생략")
    args = parser.parse_args(argv)

    try:
        home = resolve_home(args.home)
    except SettingsError as e:
        print(f"[오류] {e}", file=sys.stderr)
        return 2

    for sub in SUBDIRS:
        (home / sub).mkdir(parents=True, exist_ok=True)
    print(f"CENTRAL_HOME: {home}")

    for rel, template in TEMPLATES.items():
        target = home / rel
        if target.exists():
            print(f"  유지  {target}")
        else:
            shutil.copyfile(template, target)
            print(f"  생성  {target}")

    if sys.platform == "win32" and not args.no_acl:
        for d in RESTRICTED_DIRS:
            restrict_acl(home / d)
            print(f"  권한 제한  {home / d}")

    steps = [
        f"{home / 'secrets' / 'secrets.toml'} 에 앱키/계좌 정보 입력",
        f"{home / 'config' / 'central.toml'} 확인",
    ]
    if args.home and not os.environ.get("CENTRAL_HOME"):
        steps.append(f'환경변수 설정: setx CENTRAL_HOME "{home}"')
    steps.append("python -m central config  로 확인")
    print("\n다음 단계:")
    for i, step in enumerate(steps, 1):
        print(f"  {i}) {step}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
