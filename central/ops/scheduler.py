"""하루 일정 작업과 주기 작업.

- 일정 작업(Job): 시장 현지 시각 at 이후 하루 한 번. 서버가 늦게 떠도 until 전이면 따라잡아 실행한다
- 실행 기록은 job_runs 테이블 (작업 시간대 기준 날짜). 재시작해도 같은 날 다시 돌지 않는다
- 실패해도 그날은 실행한 것으로 기록하고 알린다 (20초마다 재시도하며 KIS 를 두드리지 않도록).
  관리자 API 로 즉시 다시 실행할 수 있다
- 주기 작업(Periodic): interval 초마다 (감시)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from central.core.market_hours import KST, now_kst
from central.storage.db import Database
from central.storage.models import JobRunRow

log = logging.getLogger(__name__)

JobFn = Callable[[], Awaitable[str]]


@dataclass
class Job:
    name: str
    at: time
    run: JobFn
    tz: ZoneInfo = KST
    when: Callable[[date], bool] = lambda d: True   # 현지 날짜 조건 (개장일 등)
    until: time = time(23, 59, 59)
    description: str = ""


@dataclass
class Periodic:
    name: str
    interval: float
    run: Callable[[], Awaitable[None]]
    last: float = field(default=0.0)


class Scheduler:
    def __init__(self, db: Database, *, clock: Callable[[], datetime] = now_kst,
                 on_failure: Callable[[str, str], None] | None = None, tick_seconds: float = 20.0) -> None:
        self.db, self.clock, self.on_failure, self.tick_seconds = db, clock, on_failure, tick_seconds
        self.jobs: dict[str, Job] = {}
        self.periodic: list[Periodic] = []
        self._task: asyncio.Task | None = None
        self._running: set[str] = set()

    def add(self, job: Job) -> None:
        self.jobs[job.name] = job

    def every(self, name: str, seconds: float, fn: Callable[[], Awaitable[None]]) -> None:
        self.periodic.append(Periodic(name, seconds, fn))

    # ───────────── 판단 ─────────────
    def due(self, job: Job, now: datetime) -> date | None:
        local = now.astimezone(job.tz)
        d = local.date()
        if not (job.at <= local.time() < job.until) or not job.when(d):
            return None
        last = self.last_run(job.name)
        if last is not None and last["run_date"] >= d:
            return None
        return d

    def last_run(self, name: str) -> dict | None:
        with self.db.session() as s:
            r = s.get(JobRunRow, name)
            return None if r is None else {"run_date": r.run_date, "finished_at": r.finished_at, "ok": r.ok,
                                            "detail": r.detail}

    # ───────────── 실행 ─────────────
    async def tick(self) -> list[str]:
        """할 일이 있으면 실행하고 실행한 작업 이름을 돌려준다 (테스트에서 직접 부른다)."""
        now = self.clock()
        ran = []
        for job in list(self.jobs.values()):
            d = self.due(job, now)
            if d is not None and job.name not in self._running:
                await self.run(job.name, d)
                ran.append(job.name)
        mono = asyncio.get_running_loop().time()
        for p in self.periodic:
            if mono - p.last >= p.interval:
                p.last = mono
                try:
                    await p.run()
                except Exception:
                    log.exception("주기 작업 실패: %s", p.name)
        return ran

    async def run(self, name: str, run_date: date | None = None) -> dict:
        job = self.jobs[name]
        run_date = run_date or self.clock().astimezone(job.tz).date()
        self._running.add(name)
        log.info("작업 시작: %s (%s)", name, run_date)
        try:
            detail, ok = await job.run(), True
        except Exception as e:
            detail, ok = f"{type(e).__name__}: {e}", False
            log.exception("작업 실패: %s", name)
            if self.on_failure:
                self.on_failure(name, detail)
        finally:
            self._running.discard(name)
        with self.db.session() as s, s.begin():
            row = s.get(JobRunRow, name) or JobRunRow(name=name)
            row.run_date, row.finished_at, row.ok, row.detail = run_date, self.clock(), ok, (detail or "")[:2000]
            s.merge(row)
        log.info("작업 끝: %s ok=%s %s", name, ok, detail)
        return {"name": name, "ok": ok, "detail": detail}

    def status(self) -> list[dict]:
        out = []
        for job in self.jobs.values():
            out.append({"name": job.name, "at": f"{job.at:%H:%M}", "tz": str(job.tz), "description": job.description,
                        "last": self.last_run(job.name), "running": job.name in self._running})
        return out

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="ops-scheduler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("스케줄러 오류")
            await asyncio.sleep(self.tick_seconds)
