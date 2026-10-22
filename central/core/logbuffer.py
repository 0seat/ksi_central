"""최근 WARNING 이상 로그를 메모리에 모아 둔다 (대시보드 오류 화면용).

메시지는 파일 로그와 같은 마스킹(central/core/logging.py)을 거친다. 서버 재시작 때 비워진다.
"""

from __future__ import annotations

import logging
import re
from collections import deque
from datetime import datetime

from central.core.logging import masker
from central.core.market_hours import KST

_DIGITS = re.compile(r"\d+")


class LogBuffer(logging.Handler):
    def __init__(self, capacity: int = 2000) -> None:
        super().__init__(level=logging.WARNING)
        self.items: deque[dict] = deque(maxlen=capacity)
        self.started_at = datetime.now(KST)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = masker.mask(record.getMessage())
            exc = None
            if record.exc_info and record.exc_info[1] is not None:
                e = record.exc_info[1]
                exc = masker.mask(f"{type(e).__name__}: {e}")[:500]
            self.items.append({
                "ts": datetime.fromtimestamp(record.created, KST).isoformat(timespec="seconds"),
                "level": record.levelname, "logger": record.name, "message": msg[:1000], "exc": exc,
            })
        except Exception:  # 로그 처리 중 예외가 다시 로그를 부르지 않게
            pass

    def recent(self, limit: int = 100, level: str | None = None) -> list[dict]:
        items = [i for i in self.items if level is None or i["level"] == level]
        return list(reversed(items[-limit:]))

    def groups(self, since: str | None = None) -> list[dict]:
        """같은 종류(로거 + 숫자를 지운 메시지 + 예외 종류)끼리 묶은 횟수. 많은 것부터."""
        out: dict[tuple, dict] = {}
        for i in self.items:
            if since and i["ts"] < since:
                continue
            exc_type = i["exc"].split(":", 1)[0] if i["exc"] else None
            key = (i["level"], i["logger"], _DIGITS.sub("#", i["message"])[:120], exc_type)
            g = out.setdefault(key, {"level": i["level"], "logger": i["logger"], "message": i["message"],
                                     "exc": i["exc"], "count": 0, "first": i["ts"], "last": i["ts"]})
            g["count"] += 1
            g["last"], g["message"], g["exc"] = i["ts"], i["message"], i["exc"]
        return sorted(out.values(), key=lambda g: (-g["count"], g["last"]))


_buffer: LogBuffer | None = None


def install() -> LogBuffer:
    """루트 로거에 한 번만 붙인다 (테스트에서 앱을 여러 번 만들어도 중복되지 않게)."""
    global _buffer
    if _buffer is None:
        _buffer = LogBuffer()
    root = logging.getLogger()
    if _buffer not in root.handlers:
        root.addHandler(_buffer)
    return _buffer
