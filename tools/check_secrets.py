"""커밋 전 비밀정보 검사 (pre-commit 로컬 훅 / CI).

    python tools/check_secrets.py FILE...     # pre-commit 이 스테이징된 파일을 넘김
    python tools/check_secrets.py --all       # git 이 추적하는 모든 파일

gitleaks 와 별개로 동작하는 1차 방어선이다. 두 가지를 검사한다.
1. 금지 파일명: 실제 설정/키/DB/토큰 캐시 파일
2. 금지 내용: 비밀 키 이름에 리터럴 값이 대입된 경우 (예: app_key = "<실제 키>")
   단, 값이 테스트용 접두어(dummy/test/fake/example)로 시작하면 허용
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
import sys
from pathlib import Path

FORBIDDEN_NAMES = [
    "secrets.toml",
    "central.toml",
    ".env",
    ".env.*",
    "*.db",
    "*.sqlite",
    "*.sqlite3",
    "*.pem",
    "*.key",
    "*.p12",
    "kis_devlp.yaml",
    "KIS2[0-9][0-9][0-9][0-9][0-9][0-9][0-9]",
]
ALLOWED_NAMES = [".env.example", "*.example.toml"]

SECRET_ASSIGNMENT = re.compile(
    r"""(?ix)
    \b(app_?key|app_?secret|secret_?key|hts_?id|account_no|cano|admin_key|bot_token|chat_id|access_token|approval_key)\b
    ["']?\s*[:=]\s*
    ["']([^"'\s]{4,})["']
    """
)
# "<실제 키>" 같은 안내 문구, "{VAR}" 같은 템플릿 치환자도 허용
PLACEHOLDER = re.compile(r"(?i)^(dummy|test|fake|example|sample|changeme|your[_-]|<|\{)")


def forbidden_name(path: Path) -> bool:
    name = path.name
    if any(fnmatch.fnmatch(name, p) for p in ALLOWED_NAMES):
        return False
    return any(fnmatch.fnmatch(name, p) for p in FORBIDDEN_NAMES)


def secret_lines(path: Path) -> list[tuple[int, str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in SECRET_ASSIGNMENT.finditer(line):
            if not PLACEHOLDER.match(m.group(2)):
                hits.append((lineno, m.group(1)))
    return hits


def tracked_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z"], check=True, capture_output=True).stdout
    return [Path(p) for p in out.decode("utf-8").split("\0") if p]


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    files = tracked_files() if args == ["--all"] else [Path(a) for a in args]

    problems: list[str] = []
    for path in files:
        if forbidden_name(path):
            problems.append(f"{path}: 커밋 금지 파일 (비밀정보/운영 데이터는 CENTRAL_HOME 에 둘 것)")
            continue
        for lineno, key in secret_lines(path):
            # 값 자체는 출력하지 않는다.
            problems.append(f"{path}:{lineno}: '{key}' 에 리터럴 값이 들어 있음")

    if problems:
        print("[check_secrets] 비밀정보 의심 항목 발견 — 커밋 차단", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
