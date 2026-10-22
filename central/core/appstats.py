"""앱(서브계좌)별 API 사용량. 최근 1시간만 메모리에 둔다 (재시작 때 비움).

요청마다 ``RequestTag`` 를 contextvar 로 걸어 두고, 인증이 끝나면 서브계좌 id 를 채운다.
그 요청을 처리하다 KIS 를 부르면 KIS 클라이언트가 같은 태그의 ``kis_calls`` 를 올린다.
그래서 캐시로 응답한 조회는 KIS 호출 0 으로 남는다.
"""

from __future__ import annotations

import time
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass

WINDOW_S = 3600


@dataclass
class RequestTag:
    sub_id: int | None = None
    kis_calls: int = 0


_current: ContextVar[RequestTag | None] = ContextVar("central_request_tag", default=None)


def current() -> RequestTag | None:
    return _current.get()


def begin() -> tuple[RequestTag, object]:
    tag = RequestTag()
    return tag, _current.set(tag)


def end(token) -> None:
    _current.reset(token)


def count_kis_call() -> None:
    """KIS REST 호출 1회. 앱 요청 처리 중이면 그 앱 몫으로 센다."""
    tag = _current.get()
    if tag is not None:
        tag.kis_calls += 1


@dataclass(frozen=True)
class Request:
    at: float
    sub_id: int
    endpoint: str
    ok: bool
    kis_calls: int
    elapsed_ms: float


class AppStats:
    def __init__(self, clock=time.time) -> None:
        self.clock = clock
        self._items: deque[Request] = deque(maxlen=200_000)

    def record(self, sub_id: int, endpoint: str, ok: bool, kis_calls: int, elapsed_ms: float) -> None:
        self._items.append(Request(self.clock(), sub_id, endpoint, ok, kis_calls, elapsed_ms))
        self._trim()

    def _trim(self) -> None:
        cut = self.clock() - WINDOW_S
        while self._items and self._items[0].at < cut:
            self._items.popleft()

    def snapshot(self, sub_ids: set[int] | None = None) -> dict[int, dict]:
        """서브계좌별: 요청/KIS 호출/오류 합계, 엔드포인트별 집계, 분당 요청 수(60개, 오래된 것부터), 마지막 요청."""
        self._trim()
        now = self.clock()
        out: dict[int, dict] = {}
        for r in self._items:
            if sub_ids is not None and r.sub_id not in sub_ids:
                continue
            a = out.setdefault(r.sub_id, {"requests": 0, "kis_calls": 0, "errors": 0, "last_at": None,
                                          "elapsed_ms_sum": 0.0, "endpoints": {}, "per_minute": [0] * 60})
            a["requests"] += 1
            a["kis_calls"] += r.kis_calls
            a["errors"] += not r.ok
            a["last_at"] = r.at
            a["elapsed_ms_sum"] += r.elapsed_ms
            e = a["endpoints"].setdefault(r.endpoint, {"requests": 0, "kis_calls": 0, "errors": 0})
            e["requests"] += 1
            e["kis_calls"] += r.kis_calls
            e["errors"] += not r.ok
            idx = 59 - int((now - r.at) // 60)
            if 0 <= idx < 60:
                a["per_minute"][idx] += 1
        for a in out.values():
            a["elapsed_avg_ms"] = round(a.pop("elapsed_ms_sum") / a["requests"], 1)
        return out


class AppStatsMiddleware:
    """/api/v1 의 앱 요청(관리자 API 제외)을 서브계좌별로 센다. 인증은 기존 의존성(require_sub)이 하고 태그만 채운다."""

    def __init__(self, app, stats: AppStats, prefix: str = "/api/v1") -> None:
        self.app, self.stats, self.prefix = app, stats, prefix

    async def __call__(self, scope, receive, send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith(self.prefix) or path.startswith(self.prefix + "/admin"):
            await self.app(scope, receive, send)
            return
        tag, token = begin()
        status = [500]
        started = time.perf_counter()

        async def send_wrapper(message) -> None:
            if message["type"] == "http.response.start":
                status[0] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            end(token)
            if tag.sub_id is not None:
                endpoint = getattr(scope.get("endpoint"), "__name__", None) or path[len(self.prefix):]
                self.stats.record(tag.sub_id, endpoint, status[0] < 400, tag.kis_calls,
                                  (time.perf_counter() - started) * 1000)
