"""로그 설정과 비밀정보 마스킹.

모든 핸들러의 최종 출력 문자열(예외 traceback 포함)에 마스킹을 적용한다.
- 등록된 비밀 값(앱키, 시크릿, 계좌번호 등)은 그대로 치환
- 등록되지 않은 값이라도 키 이름 패턴(appkey=..., Authorization: Bearer ...)은 값 부분을 치환
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
import threading
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from central.settings import Settings

MASK = "****"
_MIN_SECRET_LEN = 4

_KEY_VALUE_PATTERN = re.compile(
    r"""(?ix)
    (["']?(?:app_?key|app_?secret|secret_?key|access_token|approval_key|admin_key|bot_token|authorization|x-api-key|x-admin-key)["']?)
    (\s*[:=]\s*["']?)
    (bearer\s+)?
    ([^\s"',}&]+)
    """
)


class SecretMasker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._secrets: list[str] = []

    def add(self, values: Iterable[str]) -> None:
        with self._lock:
            merged = set(self._secrets) | {v for v in values if v and len(v) >= _MIN_SECRET_LEN}
            # 긴 값부터 치환해야 부분 문자열 충돌이 없다 (예: 계좌번호-상품코드 vs 계좌번호)
            self._secrets = sorted(merged, key=len, reverse=True)

    def clear(self) -> None:
        with self._lock:
            self._secrets = []

    def mask(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, MASK)
        return _KEY_VALUE_PATTERN.sub(lambda m: f"{m[1]}{m[2]}{m[3] or ''}{MASK}", text)


masker = SecretMasker()


class MaskingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return masker.mask(super().format(record))


_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"


def setup_logging(settings: Settings) -> None:
    masker.add(settings.secret_values())

    formatter = MaskingFormatter(_FORMAT)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)

    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.TimedRotatingFileHandler(
        settings.logs_dir / "central.log",
        when="midnight",
        backupCount=settings.logging.retention_days,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_handler)
    root.setLevel(settings.logging.level.upper())
