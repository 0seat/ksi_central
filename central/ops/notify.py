"""Discord 웹훅 알림.

- 웹훅 URL 은 secrets.toml [discord] webhook_url 에만 둔다 (URL 의 토큰이 곧 비밀)
- 보내는 일은 별도 스레드가 맡는다. 호출하는 쪽(이벤트 루프, 로그 핸들러, 감시자)은 막히지 않는다
- 같은 알림(key)은 cooldown 동안 한 번만 보내고, 생략한 건수는 다음 알림에 붙인다
- Discord 속도 제한(429)은 retry_after 만큼 기다렸다 다시 보낸다
- 본문은 로그와 같은 마스킹을 거치고, @everyone 같은 멘션은 막는다
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import traceback
from datetime import datetime
from typing import Callable

import httpx

from central.core.logging import masker
from central.core.market_hours import now_kst

log = logging.getLogger(__name__)

LEVELS = {"info": 0, "warning": 1, "error": 2}
_LABEL = {"info": "알림", "warning": "경고", "error": "오류"}
_MAX_CONTENT = 1900          # Discord 메시지 한도 2000자
_QUEUE_SIZE = 500


class Notifier:
    def __init__(
        self, webhook: str | None, *, source: str = "중앙증권", min_level: str = "info",
        cooldown_seconds: float = 900.0, transport: httpx.BaseTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic, clock: Callable[[], datetime] = now_kst,
    ) -> None:
        self._webhook = webhook
        self.source = source
        self.min_level = LEVELS[min_level]
        self.cooldown = cooldown_seconds
        self._transport = transport
        self._monotonic, self._clock = monotonic, clock
        self._q: queue.Queue[str | None] = queue.Queue(_QUEUE_SIZE)
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}
        self._suppressed: dict[str, int] = {}
        self._thread: threading.Thread | None = None
        self.sent = self.failed = self.dropped = 0

    @property
    def enabled(self) -> bool:
        return bool(self._webhook)

    # ───────────── 보내기 ─────────────
    def notify(self, level: str, title: str, body: str = "", *, key: str | None = None) -> bool:
        """보낼 대기열에 넣었으면 True (꺼져 있거나, 수준 미달이거나, 반복 억제되면 False)."""
        if not self.enabled or LEVELS[level] < self.min_level:
            return False
        key = key or f"{level}:{title}"
        now = self._monotonic()
        with self._lock:
            last = self._last.get(key)
            if last is not None and now - last < self.cooldown:
                self._suppressed[key] = self._suppressed.get(key, 0) + 1
                return False
            self._last[key] = now
            skipped = self._suppressed.pop(key, 0)
        text = self.format(level, title, body, skipped)
        try:
            self._q.put_nowait(text)
        except queue.Full:
            self.dropped += 1
            return False
        if self._thread is None:
            self.start()
        return True

    def info(self, title: str, body: str = "", **kw) -> bool:
        return self.notify("info", title, body, **kw)

    def warning(self, title: str, body: str = "", **kw) -> bool:
        return self.notify("warning", title, body, **kw)

    def error(self, title: str, body: str = "", **kw) -> bool:
        return self.notify("error", title, body, **kw)

    def format(self, level: str, title: str, body: str, skipped: int = 0) -> str:
        lines = [f"**[{self.source}] {_LABEL[level]} | {title}**"]
        if body:
            lines.append(body.strip())
        tail = f"{self._clock():%Y-%m-%d %H:%M:%S} KST"
        if skipped:
            tail += f" · 같은 알림 {skipped}건 생략"
        lines.append(f"-# {tail}")
        text = masker.mask("\n".join(lines))
        if len(text) > _MAX_CONTENT:
            text = text[:_MAX_CONTENT - 20] + "\n...(생략)"
        return text

    # ───────────── 전송 스레드 ─────────────
    def start(self) -> None:
        with self._lock:
            if self._thread is None and self.enabled:
                self._thread = threading.Thread(target=self._run, name="discord-notifier", daemon=True)
                self._thread.start()

    def close(self, timeout: float = 10.0) -> None:
        """대기 중인 알림을 다 보내고(최대 timeout 초) 스레드를 끝낸다."""
        if self._thread is None:
            return
        try:
            self._q.put(None, timeout=1)
        except queue.Full:
            pass
        self._thread.join(timeout)
        self._thread = None

    def _run(self) -> None:
        with httpx.Client(timeout=10.0, transport=self._transport) as http:
            while True:
                item = self._q.get()
                if item is None:
                    return
                # 쌓여 있으면 한 메시지로 묶는다 (속도 제한 절약)
                batch, stop = item, False
                while True:
                    try:
                        nxt = self._q.get_nowait()
                    except queue.Empty:
                        break
                    if nxt is None:
                        stop = True
                        break
                    if len(batch) + len(nxt) + 2 > _MAX_CONTENT:
                        self._post(http, batch)
                        batch = nxt
                    else:
                        batch += "\n\n" + nxt
                self._post(http, batch)
                if stop:
                    return

    def _post(self, http: httpx.Client, content: str) -> None:
        payload = {"content": content, "username": self.source, "allowed_mentions": {"parse": []}}
        for attempt in range(4):
            try:
                res = http.post(self._webhook, json=payload)  # type: ignore[arg-type]
            except httpx.HTTPError as e:
                log.warning("Discord 전송 실패 (%s), 재시도 %d", type(e).__name__, attempt + 1)
                time.sleep(2 * (attempt + 1))
                continue
            if res.status_code < 300:
                self.sent += 1
                time.sleep(0.5)   # 웹훅 한도: 2초에 5건
                return
            if res.status_code == 429:
                try:
                    wait = float(res.json().get("retry_after", 2))
                except ValueError:
                    wait = 2.0
                time.sleep(min(max(wait, 0.5), 60))
                continue
            if res.status_code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            log.warning("Discord 전송 거부 HTTP %d (웹훅 URL 확인)", res.status_code)
            break
        self.failed += 1


class DiscordLogHandler(logging.Handler):
    """ERROR 이상 로그를 알림으로 보낸다. 같은 위치의 같은 메시지 틀은 cooldown 동안 한 번."""

    def __init__(self, notifier: Notifier, level: int = logging.ERROR) -> None:
        super().__init__(level)
        self.notifier = notifier

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(__name__):
            return   # 알림 실패 로그가 다시 알림이 되지 않게
        try:
            body = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                body += "\n" + "".join(traceback.format_exception_only(record.exc_info[1])).strip()
            self.notifier.error(f"오류 로그 ({record.name})", f"```\n{body[:1500]}\n```",
                                key=f"log:{record.name}:{record.msg}")
        except Exception:
            self.handleError(record)


def notifier_from_settings(settings, *, source: str = "중앙증권") -> Notifier:
    hook = settings.discord_webhook.get_secret_value() if settings.discord_webhook else None
    return Notifier(hook, source=source, min_level=settings.ops.notify_min_level,
                    cooldown_seconds=settings.ops.alert_cooldown_minutes * 60)
