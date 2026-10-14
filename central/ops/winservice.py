"""Windows 작업 스케줄러로 감시자를 상시 실행한다.

    python -m central service install     등록 (관리자 권한 PowerShell 필요)
    python -m central service start       켜고 바로 실행
    python -m central service stop        끄고 정상 종료 (자동 재실행도 멈춤)
    python -m central service status
    python -m central service uninstall

등록 내용
- 실행: <venv python> -m central supervise (작업 폴더 = 저장소)
- 계정: 현재 사용자, 로그온 여부와 무관 (S4U: 암호를 저장하지 않음. 로컬 파일과 인터넷만 쓰므로 충분)
- 트리거: 부팅 30초 후 + 5분마다. 이미 실행 중이면 새로 띄우지 않으므로(IgnoreNew)
  감시자가 어떤 이유로 끝나도 5분 안에 다시 뜬다
- 실행 시간 제한 없음, 배터리/유휴 조건 없음

NSSM/WinSW 대신 이 방식을 쓴 이유: 외부 실행 파일 없이 Windows 기본 기능만 쓰고,
재시작과 알림은 감시자(supervisor.py)가 직접 맡는다.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from xml.sax.saxutils import escape

from central.ops.supervisor import status_path, stop_request_path
from central.settings import REPO_ROOT, Settings

TASK_NAME = "CentralSecurities"


def task_xml(python: str, workdir: Path, user: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>중앙증권 서버 감시자 (python -m central supervise)</Description>
  </RegistrationInfo>
  <Triggers>
    <BootTrigger>
      <Enabled>true</Enabled>
      <Delay>PT30S</Delay>
    </BootTrigger>
    <TimeTrigger>
      <Repetition>
        <Interval>PT5M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>2026-01-01T00:00:00</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>S4U</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>5</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(python)}</Command>
      <Arguments>-m central supervise</Arguments>
      <WorkingDirectory>{escape(str(workdir))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks", *args], capture_output=True, text=True, encoding="mbcs", errors="replace")


def install(settings: Settings) -> int:
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ['USERNAME']}".lstrip("\\")
    xml = task_xml(sys.executable, REPO_ROOT, user)
    path = settings.state_dir / "service-task.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(xml, encoding="utf-16")
    r = _schtasks("/Create", "/TN", TASK_NAME, "/XML", str(path), "/F")
    if r.returncode != 0:
        print(f"등록 실패: {(r.stderr or r.stdout).strip()}", file=sys.stderr)
        print("관리자 권한 PowerShell 에서 다시 실행하세요:\n"
              f"  cd {REPO_ROOT}\n  .\\.venv\\Scripts\\python.exe -m central service install", file=sys.stderr)
        return 1
    print(f"작업 등록: {TASK_NAME} (계정 {user}, 부팅 시 + 5분마다 확인)\n"
          f"  실행: {sys.executable} -m central supervise\n  시작: python -m central service start")
    return 0


def uninstall(settings: Settings) -> int:
    stop(settings)
    r = _schtasks("/Delete", "/TN", TASK_NAME, "/F")
    print((r.stdout or r.stderr).strip())
    return r.returncode


def start(settings: Settings) -> int:
    """작업을 켜고 실행. 콘솔에서 직접 띄운 감시자가 있으면 먼저 정상 종료시켜 작업으로 넘긴다."""
    st = read_status(settings)
    if st and st.get("running"):
        print(f"실행 중인 감시자(pid {st['pid']})를 정상 종료한 뒤 작업으로 시작합니다")
        if not _request_stop(settings, 60):
            print("기존 감시자가 끝나지 않음. 수동으로 확인 필요", file=sys.stderr)
            return 1
    for args in (("/Change", "/TN", TASK_NAME, "/ENABLE"), ("/Run", "/TN", TASK_NAME)):
        r = _schtasks(*args)
        if r.returncode != 0:
            print((r.stderr or r.stdout).strip(), file=sys.stderr)
            return r.returncode
    print("시작 요청. 상태: python -m central service status")
    return 0


def stop(settings: Settings, wait: float = 60) -> int:
    """자동 재실행을 끄고(작업 비활성) 감시자에게 정상 종료를 요청한다."""
    _schtasks("/Change", "/TN", TASK_NAME, "/DISABLE")
    st = read_status(settings)
    if not st or not st.get("running"):
        print("감시자가 실행 중이 아닙니다 (작업은 비활성화함)")
        return 0
    if _request_stop(settings, wait):
        print("정상 종료됨 (작업 비활성. 다시 켜려면 service start)")
        return 0
    print(f"{wait:.0f}초 안에 끝나지 않음. 작업 강제 종료", file=sys.stderr)
    _schtasks("/End", "/TN", TASK_NAME)
    return 1


def _request_stop(settings: Settings, wait: float) -> bool:
    stop_request_path(settings).touch()
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        st = read_status(settings)
        if st and not st.get("running"):
            return True
        time.sleep(1)
    return False


def read_status(settings: Settings) -> dict | None:
    try:
        return json.loads(status_path(settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def status(settings: Settings) -> int:
    r = _schtasks("/Query", "/TN", TASK_NAME, "/FO", "LIST")
    print(r.stdout.strip() if r.returncode == 0 else "작업 미등록 (python -m central service install)")
    st = read_status(settings)
    if st:
        print(f"\n감시자: {'실행 중' if st['running'] else '종료됨'} pid={st['pid']} (갱신 {st['updated_at']})")
        for name, c in st["children"].items():
            print(f"  {name:<10} {'실행' if c['running'] else '중지'} pid={c['pid']} 시작 {c['started_at']} "
                  f"재시작 {c['restarts']}회 마지막 종료코드 {c['last_exit']}")
    try:
        import httpx
        host = settings.server.hosts[0]
        h = httpx.get(f"http://{host}:{settings.server.port}/api/v1/health", timeout=3).json()["data"]
        print(f"\nAPI: {h}")
    except Exception as e:
        print(f"\nAPI 응답 없음 ({type(e).__name__})")
    return 0
