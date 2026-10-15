"""프로세스 감시자: python -m central supervise

- 서버(serve)와 설정의 추가 프로그램(ops.programs)을 자식 프로세스로 띄우고, 끝나면 다시 띄운다.
  추가 프로그램은 저장소 밖 아무 폴더의 실행 파일이어도 된다 (command, cwd). 서버 주소는 CENTRAL_API_URL 로 준다
- 재시작 간격은 5초부터 두 배씩 최대 5분. 10분 넘게 살아 있었으면 다시 5초부터
- 자식이 끝나면 Discord 로 알린다 (종료 코드, 콘솔 출력 마지막 줄)
- 한 번에 하나만 실행한다 (state/supervisor.lock 잠금)
- Windows Job Object(KILL_ON_JOB_CLOSE)에 자신을 넣어, 감시자가 강제 종료되면 자식도 함께 끝난다.
  고아 서버가 앱키의 실시간 세션을 쥐고 남으면 새 서버가 OPSP8996 으로 연결하지 못하기 때문
- 종료: state/supervisor.stop 파일 → 자식마다 state/stop/<이름> 파일로 정상 종료 요청 → 30초 뒤 강제 종료
  (콘솔 없는 프로세스에는 Ctrl+C 를 보낼 수 없어 파일로 알린다. 자식은 CENTRAL_STOP_FILE 을 본다)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from central.core.market_hours import now_kst
from central.ops.notify import Notifier, notifier_from_settings
from central.settings import REPO_ROOT, Settings

log = logging.getLogger(__name__)

STOP_GRACE_SECONDS = 30
_BACKOFF_MIN, _BACKOFF_MAX, _HEALTHY_AFTER = 5.0, 300.0, 600.0
_CONSOLE_LOG_MAX = 20 * 1024 * 1024


def stop_request_path(settings: Settings) -> Path:
    return settings.state_dir / "supervisor.stop"


def status_path(settings: Settings) -> Path:
    return settings.state_dir / "supervisor.json"


@dataclass
class Child:
    name: str
    argv: list[str]
    stop_file: Path
    console_log: Path
    cwd: Path = REPO_ROOT
    extra_env: dict[str, str] = field(default_factory=dict)
    proc: subprocess.Popen | None = None
    started: float = 0.0
    started_at: datetime | None = None
    next_start: float = 0.0
    backoff: float = _BACKOFF_MIN
    restarts: int = 0
    last_exit: int | None = None
    _out: object = field(default=None, repr=False)

    def start(self) -> None:
        self.stop_file.parent.mkdir(parents=True, exist_ok=True)
        self.stop_file.unlink(missing_ok=True)
        self.console_log.parent.mkdir(parents=True, exist_ok=True)
        if self.console_log.exists() and self.console_log.stat().st_size > _CONSOLE_LOG_MAX:
            self.console_log.replace(self.console_log.with_suffix(".log.1"))
        self._out = self.console_log.open("ab")
        self._out.write(f"\n===== {now_kst():%Y-%m-%d %H:%M:%S} start {self.name} =====\n".encode())
        self._out.flush()
        env = {**os.environ, **self.extra_env, "CENTRAL_STOP_FILE": str(self.stop_file), "CENTRAL_SUPERVISED": "1",
               "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        self.proc = subprocess.Popen(self.argv, cwd=self.cwd, env=env, stdin=subprocess.DEVNULL,
                                     stdout=self._out, stderr=subprocess.STDOUT, creationflags=flags)
        self.started, self.started_at = time.monotonic(), now_kst()
        log.info("시작 %s pid=%d", self.name, self.proc.pid)

    def reap(self) -> int | None:
        """끝났으면 종료 코드, 아니면 None."""
        if self.proc is None:
            return None
        code = self.proc.poll()
        if code is None:
            return None
        self.proc = None
        self.last_exit = code
        if self._out:
            self._out.close()
            self._out = None
        return code

    def tail(self, lines: int = 15) -> str:
        try:
            with self.console_log.open("rb") as f:
                f.seek(max(0, self.console_log.stat().st_size - 8000))
                text = f.read().decode("utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(text.splitlines()[-lines:])

    def view(self) -> dict:
        return {"pid": self.proc.pid if self.proc else None, "running": self.proc is not None,
                "started_at": self.started_at.isoformat() if self.started_at else None,
                "restarts": self.restarts, "last_exit": self.last_exit}


class Supervisor:
    def __init__(self, settings: Settings, notifier: Notifier | None = None) -> None:
        self.settings = settings
        self.notifier = notifier or notifier_from_settings(settings, source="중앙증권 감시자")
        stop_dir = settings.state_dir / "stop"
        host = settings.server.hosts[0]
        api = {"CENTRAL_API_URL": f"http://{f'[{host}]' if ':' in host else host}:{settings.server.port}",
               "CENTRAL_HOME": str(settings.home)}

        def child(name: str, argv: list[str], cwd: Path = REPO_ROOT, env: dict[str, str] | None = None) -> Child:
            return Child(name, argv, stop_dir / name, settings.logs_dir / f"{name}.console.log", cwd,
                         {**api, **(env or {})})

        self.children = [child("serve", [sys.executable, "-m", "central", "serve", "--no-console-log"])]
        self.children += [child(p.name, p.argv(sys.executable), p.workdir(), p.env) for p in settings.ops.programs]
        self.stop_file = stop_request_path(settings)
        self._lock_fd: int | None = None
        self._job = None

    # ───────────── 실행 ─────────────
    def run(self) -> int:
        if not self._acquire_lock():
            print("감시자가 이미 실행 중입니다 (state/supervisor.lock)", file=sys.stderr)
            return 1
        self._job = _kill_children_with_me()
        self.stop_file.unlink(missing_ok=True)
        log.info("감시자 시작 pid=%d: %s", os.getpid(), [c.name for c in self.children])
        self.notifier.info("감시자 시작", f"프로그램: {', '.join(c.name for c in self.children)}")
        reason = "종료 요청"
        try:
            while not self.stop_file.exists():
                self.step()
                self._write_status()
                time.sleep(1)
        except KeyboardInterrupt:
            reason = "Ctrl+C"
        finally:
            self.shutdown()
            self.notifier.info("감시자 종료", reason)
            self.notifier.close()
            self.stop_file.unlink(missing_ok=True)
            self._write_status(stopped=True)
        return 0

    def step(self) -> None:
        mono = time.monotonic()
        for c in self.children:
            code = c.reap()
            if code is not None:
                lived = mono - c.started
                c.backoff = _BACKOFF_MIN if lived >= _HEALTHY_AFTER else min(c.backoff * 2, _BACKOFF_MAX)
                c.next_start = mono + c.backoff
                c.restarts += 1
                log.error("%s 종료 (코드 %s, %.0f초 실행). %.0f초 후 재시작", c.name, code, lived, c.backoff)
                self.notifier.error(f"프로세스 종료: {c.name}",
                                    f"종료 코드 {code}, {lived:.0f}초 실행 후. {c.backoff:.0f}초 뒤 다시 띄움\n"
                                    f"```\n{c.tail()[-1200:]}\n```", key=f"exit:{c.name}")
            if c.proc is None and mono >= c.next_start:
                try:
                    c.start()
                except OSError as e:
                    c.next_start = mono + _BACKOFF_MAX
                    self.notifier.error(f"프로세스 시작 실패: {c.name}", str(e), key=f"spawn:{c.name}")

    def shutdown(self) -> None:
        running = [c for c in self.children if c.proc is not None]
        for c in running:
            c.stop_file.parent.mkdir(parents=True, exist_ok=True)
            c.stop_file.touch()
        deadline = time.monotonic() + STOP_GRACE_SECONDS
        while time.monotonic() < deadline and any(c.proc and c.proc.poll() is None for c in running):
            time.sleep(0.5)
        for c in running:
            if c.proc and c.proc.poll() is None:
                log.warning("%s 가 %d초 안에 끝나지 않아 강제 종료", c.name, STOP_GRACE_SECONDS)
                c.proc.kill()
                c.proc.wait(10)
            c.reap()
            c.stop_file.unlink(missing_ok=True)

    # ───────────── 보조 ─────────────
    def _acquire_lock(self) -> bool:
        path = self.settings.state_dir / "supervisor.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._lock_fd = fd   # 프로세스가 끝나면 OS 가 잠금을 푼다
        return True

    def _write_status(self, stopped: bool = False) -> None:
        data = {"pid": os.getpid(), "running": not stopped, "updated_at": now_kst().isoformat(),
                "children": {c.name: c.view() for c in self.children}}
        path = status_path(self.settings)
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass


def _kill_children_with_me():
    """감시자를 KILL_ON_JOB_CLOSE Job 에 넣는다. 이후 만드는 자식도 같은 Job 에 들어가므로
    감시자 프로세스가 어떤 이유로 끝나든 자식이 함께 정리된다. 핸들을 돌려받아 프로세스 끝까지 쥐고 있어야 한다."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BasicLimit(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class ExtendedLimit(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimit), ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.GetCurrentProcess.restype = wintypes.HANDLE

    job = k32.CreateJobObjectW(None, None)
    info = ExtendedLimit()
    info.BasicLimitInformation.LimitFlags = 0x2000   # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not job or not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        log.warning("Job Object 설정 실패 (%d): 감시자 강제 종료 시 자식이 남을 수 있음", ctypes.get_last_error())
        return None
    if not k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()):
        log.warning("Job Object 등록 실패 (%d): 감시자 강제 종료 시 자식이 남을 수 있음", ctypes.get_last_error())
        return None
    return job
