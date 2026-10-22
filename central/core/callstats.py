"""외부 API 호출 통계 (대시보드 시스템 화면용). 최근 1시간만 메모리에 둔다.

호출 1건 = HTTP 요청 1회 (호출 한도 초과 재시도도 1건으로 센다).
분류(kind): order(주문/정정/취소), account(잔고/체결 조회), quote(시세), ranking(순위/수급), other.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

WINDOW_S = 3600


@dataclass(frozen=True)
class Call:
    at: float          # time.time()
    kind: str
    tr_id: str
    wait_ms: float     # 호출 간격 제한(RateLimiter) 대기
    elapsed_ms: float  # HTTP 왕복
    ok: bool
    rate_limited: bool


def classify(path: str, method: str) -> str:
    if "/trading/" in path:
        return "order" if method == "POST" else "account"
    if "/ranking/" in path or "investor" in path or "program" in path:
        return "ranking"
    if "/quotations/" in path:
        return "quote"
    return "other"


class CallStats:
    def __init__(self, clock=time.time) -> None:
        self.clock = clock
        self._calls: deque[Call] = deque(maxlen=200_000)

    def record(self, kind: str, tr_id: str, wait_ms: float, elapsed_ms: float, ok: bool, rate_limited: bool) -> None:
        self._calls.append(Call(self.clock(), kind, tr_id, wait_ms, elapsed_ms, ok, rate_limited))
        self._trim()

    def _trim(self) -> None:
        cut = self.clock() - WINDOW_S
        while self._calls and self._calls[0].at < cut:
            self._calls.popleft()

    def snapshot(self) -> dict:
        """최근 1/10/60분 집계, 분류별 집계, 분당 호출 수(60개, 오래된 것부터)."""
        self._trim()
        now = self.clock()
        calls = list(self._calls)

        def agg(items: list[Call]) -> dict:
            n = len(items)
            waits = sorted(c.wait_ms for c in items)
            return {
                "calls": n,
                "errors": sum(not c.ok for c in items),
                "rate_limited": sum(c.rate_limited for c in items),
                "wait_avg_ms": round(sum(waits) / n, 1) if n else 0,
                "wait_p95_ms": round(waits[min(n - 1, int(n * 0.95))], 1) if n else 0,
                "elapsed_avg_ms": round(sum(c.elapsed_ms for c in items) / n, 1) if n else 0,
            }

        windows = {f"{m}m": agg([c for c in calls if c.at >= now - m * 60]) for m in (1, 10, 60)}
        kinds = sorted({c.kind for c in calls})
        per_minute = [0] * 60
        per_minute_kind = {k: [0] * 60 for k in kinds}
        for c in calls:
            i = 59 - int((now - c.at) // 60)
            if 0 <= i < 60:
                per_minute[i] += 1
                per_minute_kind[c.kind][i] += 1
        return {"windows": windows, "by_kind": {k: agg([c for c in calls if c.kind == k]) for k in kinds},
                "per_minute": per_minute, "per_minute_kind": per_minute_kind}
