"""M6: 알림, 스케줄러, 정지 영속화, 운영 작업, 감시자, 파일럿 로직."""

from __future__ import annotations

import json
import sys
import time as _time
import zipfile
from datetime import date, datetime, time, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from apps.pilot.runner import Pilot
from apps.pilot.strategy import BarCheck, SignalState
from central.app import CentralApp
from central.broker.sim import SimBroker
from central.core.logging import masker
from central.core.market_hours import KST, NEW_YORK, MarketHours
from central.oms.orders import PlaceOrder
from central.ops.notify import Notifier
from central.ops.scheduler import Job, Scheduler
from central.ops.supervisor import Supervisor
from central.risk.engine import RiskEngine
from central.settings import ProgramConfig, SettingsError, load_settings
from central.storage.db import Database
from tests.conftest import SECRETS_TOML, write_home
from tests.oms_env import Clock

HOOK = "https://discord.com/api/webhooks/123456789/abcDEF-token_value"


class RecNotifier(Notifier):
    def __init__(self) -> None:
        super().__init__(HOOK)
        self.items: list[tuple[str, str, str]] = []

    def notify(self, level: str, title: str, body: str = "", *, key: str | None = None) -> bool:
        self.items.append((level, title, body))
        return True

    def titles(self, level: str | None = None) -> list[str]:
        return [t for lv, t, _ in self.items if level is None or lv == level]


# ───────────── 알림 ─────────────
def test_notifier_cooldown_mask_and_retry() -> None:
    sent: list[dict] = []
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"retry_after": 0.01})
        sent.append(json.loads(req.content))
        return httpx.Response(204)

    t = {"now": 0.0}
    n = Notifier(HOOK, transport=httpx.MockTransport(handler), cooldown_seconds=60, monotonic=lambda: t["now"])
    masker.add(["SECRET-VALUE-1234"])
    try:
        assert n.error("대사 불일치", "앱키 SECRET-VALUE-1234 노출 시험") is True
        assert n.error("대사 불일치", "같은 알림") is False          # cooldown 안
        t["now"] = 61
        assert n.error("대사 불일치", "다시") is True
        assert n.info("수준 미달 아님") is True
        n.close()
    finally:
        masker.clear()
    text = "\n".join(m["content"] for m in sent)
    assert "SECRET-VALUE-1234" not in text and "****" in text
    assert "같은 알림 1건 생략" in text
    assert all(m["allowed_mentions"] == {"parse": []} for m in sent)
    assert calls["n"] >= 2 and n.sent >= 1 and n.failed == 0


def test_notifier_disabled_and_level_filter() -> None:
    assert Notifier(None).error("x") is False
    n = Notifier(HOOK, min_level="error", transport=httpx.MockTransport(lambda r: httpx.Response(204)))
    assert n.warning("경고") is False and n.error("오류") is True
    n.close()


def test_webhook_url_masked_in_logs() -> None:
    assert "abcDEF" not in masker.mask(f"POST {HOOK} 실패")


def test_discord_webhook_and_programs_validation(tmp_path: Path) -> None:
    write_home(tmp_path, secrets=SECRETS_TOML + '\n[discord]\nwebhook_url = "https://evil.example.com/hook"\n')
    with pytest.raises(SettingsError, match="discord_webhook"):
        load_settings(tmp_path)
    write_home(tmp_path, secrets=SECRETS_TOML + f'\n[discord]\nwebhook_url = "{HOOK}"\n')
    s = load_settings(tmp_path)
    assert s.discord_webhook.get_secret_value() == HOOK and HOOK in s.secret_values()
    with pytest.raises(ValueError):
        ProgramConfig(name="Bad Name", args=["-m", "x"])


# ───────────── 스케줄러 ─────────────
async def test_scheduler_runs_once_per_day_with_catch_up_and_persists() -> None:
    db = Database.memory()
    db.create_all()
    clock = Clock(datetime(2026, 10, 14, 6, 59, tzinfo=KST))
    ran: list[str] = []

    async def job() -> str:
        ran.append("a")
        return "done"

    def make() -> Scheduler:
        s = Scheduler(db, clock=clock)
        s.add(Job("a", time(7, 0), job, until=time(12, 0)))
        s.add(Job("weekday", time(7, 0), job, when=lambda d: d.weekday() < 5))
        return s

    s = make()
    assert await s.tick() == []
    clock.t = datetime(2026, 10, 14, 9, 30, tzinfo=KST)          # 늦게 떠도 until 전이면 실행
    assert sorted(await s.tick()) == ["a", "weekday"]
    assert await make().tick() == []                              # 재시작해도 같은 날은 다시 안 돎
    clock.t = datetime(2026, 10, 15, 13, 0, tzinfo=KST)           # 다음 날 until 이후: a 는 건너뜀
    assert await s.tick() == ["weekday"]
    clock.t = datetime(2026, 10, 17, 8, 0, tzinfo=KST)            # 토요일
    assert await s.tick() == ["a"]
    assert s.last_run("a")["detail"] == "done"


async def test_scheduler_failure_recorded_and_reported() -> None:
    db = Database.memory()
    db.create_all()
    clock = Clock(datetime(2026, 10, 14, 16, 30, tzinfo=NEW_YORK))
    failures: list[str] = []

    async def boom() -> str:
        raise RuntimeError("KIS 응답 없음")

    s = Scheduler(db, clock=clock, on_failure=lambda name, d: failures.append(f"{name}:{d}"))
    s.add(Job("us_close", time(16, 20), boom, tz=NEW_YORK))
    assert await s.tick() == ["us_close"]
    assert failures == ["us_close:RuntimeError: KIS 응답 없음"]
    assert s.last_run("us_close")["ok"] is False and s.last_run("us_close")["run_date"] == date(2026, 10, 14)
    assert await s.tick() == []                                   # 같은 날 재시도하지 않음 (관리자가 다시 실행)


# ───────────── 정지 영속화 ─────────────
def test_halts_survive_restart() -> None:
    db = Database.memory()
    db.create_all()
    r = RiskEngine(db)
    r.halt("broker_market", "real:KR")
    r.halt("all")
    r.resume("all")
    assert RiskEngine(db).halted == {("broker_market", "real:KR")}


# ───────────── 운영 작업 ─────────────
def make_app(home: Path, clock: Clock, *, watchlist: list[str] | None = None, db: Database | None = None):
    settings = load_settings(home)
    if watchlist:
        settings = settings.model_copy(update={"market_data": settings.market_data.model_copy(
            update={"watchlist": watchlist})})
    real, paper = SimBroker("real", clock=clock), SimBroker("paper", clock=clock)
    rec = RecNotifier()
    app = CentralApp(settings, db=db or Database.memory(), brokers={"real": real, "paper": paper}, clock=clock,
                     realtime={}, notifier=rec)
    return app, real, paper, rec


async def test_kr_close_reconciles_reports_and_alerts_mismatch(home: Path) -> None:
    clock = Clock(datetime(2026, 10, 14, 15, 50, tzinfo=KST))
    app, real, paper, rec = make_app(home, clock)
    real.deposit("KRW", D("-1000"))                    # 미배정에 없는 돈이 빠져나감 → real/KR 불일치
    result = await app.ops.scheduler.run("kr_close")
    assert result["ok"] and "paper/KR 대사 일치" in result["detail"] and "real/KR 대사 불일치" in result["detail"]
    assert "대사 불일치 real/KR" in rec.titles("error")
    assert ("broker_market", "real:KR") in app.risk.halted
    report = next(b for lv, t, b in rec.items if t.startswith("일일 보고"))
    assert "real:KR" in report and "주문/체결 없음" in report
    assert app.ops.scheduler.last_run("kr_close")["run_date"] == date(2026, 10, 14)
    await app.http.aclose()


async def test_kr_expire_releases_open_orders(home: Path) -> None:
    clock = Clock(datetime(2026, 10, 14, 10, 0, tzinfo=KST))
    app, real, paper, rec = make_app(home, clock)
    paper.set_quote("KR:005930", D("61000"), upper=D("79300"), lower=D("42700"))
    paper.deposit("KRW", D("1000000"))
    app.accounts.deposit_unallocated("paper", "KRW", D("1000000"))
    sub = app.accounts.create_sub_account("paper", "A", markets=["KR"])
    app.accounts.transfer(app.accounts.unallocated("paper").id, sub.id, "KRW", D("500000"))
    app.set_trading_enabled("paper", True)
    app.catalog.resolve("KR:005930")                   # API 가 주문 전에 하는 종목 등록
    o = await app.orders.place(sub.id, PlaceOrder("c1", "KR:005930", "buy", "limit", 1, D("60000"), None))
    assert o.status == "ACCEPTED" and app.accounts.balances(sub.id)["KRW"].held > 0
    clock.t = datetime(2026, 10, 14, 20, 15, tzinfo=KST)
    result = await app.ops.scheduler.run("kr_expire")
    assert "paper 소멸 1건" in result["detail"]
    assert app.orders.get(sub.id, o.id).status == "EXPIRED" and app.accounts.balances(sub.id)["KRW"].held == 0
    await app.http.aclose()


async def test_watchdog_ws_down_and_stale_ticks(home: Path) -> None:
    clock = Clock(datetime(2026, 10, 14, 10, 0, tzinfo=KST))
    app, real, paper, rec = make_app(home, clock, watchlist=["KR:005930"])
    app.ops.started_at = clock.t
    paper.health = lambda: {"connected": False, "realtime": {"last_error": "ConnectionClosed"}}
    await app.ops.watch()
    assert rec.items == []                                        # 끊긴 직후에는 알리지 않음
    clock.t += timedelta(seconds=130)
    await app.ops.watch()
    await app.ops.watch()
    assert rec.titles("error").count("실시간 세션 끊김 paper") == 1
    paper.health = lambda: {"connected": True}
    clock.t += timedelta(minutes=5)
    await app.ops.watch()
    assert "실시간 세션 복구 paper" in rec.titles("info")
    assert "시세 수신 멈춤 KR:005930" in rec.titles("error")     # 시작 후 5분 넘게 체결 없음
    app.market.last_tick["KR:005930"] = SimpleNamespace(ts=clock.t, price=D("61000"))
    await app.ops.watch()
    assert "시세 수신 재개 KR:005930" in rec.titles("info")
    clock.t = datetime(2026, 10, 14, 16, 0, tzinfo=KST)          # 장 마감 후에는 멈춤 알림 없음
    n = len(rec.items)
    await app.ops.watch()
    assert len(rec.items) == n
    await app.http.aclose()


async def test_maintenance_backup_and_tick_compact(home: Path) -> None:
    clock = Clock(datetime(2026, 10, 15, 7, 0, tzinfo=KST))
    settings = load_settings(home)
    db = Database.file(settings.data_dir / "central.db")
    app, real, paper, rec = make_app(home, clock, db=db)
    from central.marketdata.realtime import Tick
    app.ticks.on_market("tick:KR:005930", Tick("KR:005930", datetime(2026, 10, 14, 10, 0, tzinfo=KST), D("61000"),
                                               3, 100, None, None, "buy", "KRX"))
    app.ticks.flush()
    detail = await app.ops.maintenance()
    assert "틱 합치기 1종목" in detail and "백업 central-" in detail
    day_dir = settings.ticks_dir / "KR" / "20261014" / "005930"
    assert [p.name for p in day_dir.iterdir()] == ["ticks.parquet"]
    zips = sorted(settings.backups_dir.glob("central-*.db.zip"))
    assert len(zips) == 1 and zipfile.ZipFile(zips[0]).namelist()[0].endswith(".db")
    db.engine.dispose()
    await app.http.aclose()


async def test_event_alerts(home: Path) -> None:
    clock = Clock(datetime(2026, 10, 14, 10, 0, tzinfo=KST))
    app, real, paper, rec = make_app(home, clock)
    app.halt("all")
    app.resume("all")
    app.ops.on_market("dropped:tick:US:AAPL", "[OPSP0008] MAX SUBSCRIBE OVER")
    assert rec.titles() == ["긴급 정지 all", "정지 해제 all", "시세 채널 중단 tick:US:AAPL"]
    await app.http.aclose()


# ───────────── 감시자 ─────────────
def test_supervisor_restarts_and_stops_children(home: Path) -> None:
    import sys

    settings = load_settings(home)
    outside = home.parent / "elsewhere"            # 저장소 밖 폴더의 프로그램
    outside.mkdir()
    polite = ("import os, time, pathlib; p = pathlib.Path(os.environ['CENTRAL_STOP_FILE'])\n"
              "print('cwd', pathlib.Path.cwd().name, os.environ['CENTRAL_API_URL'], os.environ['BOT_MODE'])\n"
              "while not p.exists(): time.sleep(0.1)\nprint('bye')")
    settings = settings.model_copy(update={"ops": settings.ops.model_copy(update={"programs": [
        ProgramConfig(name="polite", command=[sys.executable, "-c", polite], cwd=outside, env={"BOT_MODE": "x"})]})})
    rec = RecNotifier()
    sup = Supervisor(settings, notifier=rec)
    crash = "import sys; print('boom'); sys.exit(3)"
    sup.children[0].argv = [sys.executable, "-c", crash]
    sup.step()
    crashing = sup.children[0]
    crashing.proc.wait(30)
    sup.step()
    assert crashing.restarts == 1 and crashing.last_exit == 3 and crashing.proc is None
    assert any(t == "프로세스 종료: serve" and "boom" in b for _, t, b in rec.items)
    t0 = _time.monotonic()
    sup.shutdown()
    assert _time.monotonic() - t0 < 20                          # 종료 파일로 정상 종료 (강제 종료 아님)
    assert sup.children[1].last_exit == 0 and "bye" in sup.children[1].tail()
    assert "cwd elsewhere http://127.0.0.1:" in sup.children[1].tail() and " x" in sup.children[1].tail()


def test_program_config_command_or_args() -> None:
    with pytest.raises(ValueError):
        ProgramConfig(name="both", args=["-m", "x"], command=["a.exe"])
    with pytest.raises(ValueError):
        ProgramConfig(name="none")
    with pytest.raises(ValueError):
        ProgramConfig(name="rel", command=["a.exe"], cwd="relative/dir")
    venv = ProgramConfig(name="bot", command=["C:/bots/a/.venv/Scripts/python.exe", "bot.py"])
    assert venv.workdir() == Path("C:/bots/a") and venv.argv("py") == ["C:/bots/a/.venv/Scripts/python.exe", "bot.py"]
    assert ProgramConfig(name="exe", command=["C:/bots/b/run.exe"]).workdir() == Path("C:/bots/b")
    assert ProgramConfig(name="mod", args=["-m", "apps.pilot"]).argv("py") == ["py", "-m", "apps.pilot"]


# ───────────── 파일럿 로직 ─────────────
def test_signal_rules() -> None:
    st = SignalState(window=3, entry_bp=D("10"), max_hold=timedelta(minutes=20), max_entries=1)
    now = datetime(2026, 10, 14, 10, 0, tzinfo=KST)
    assert st.push(D("100")) is None and st.push(D("100")) is None
    sma = st.push(D("99"))                                       # sma 99.67, 99 <= 99.57
    assert st.decide(D("99"), sma, 0, now) == "buy"
    st.entries_today, st.entered_at = 1, now
    assert st.decide(D("99"), sma, 0, now) is None                # 하루 진입 한도
    assert st.decide(D("99"), sma, 1, now + timedelta(minutes=5)) is None
    assert st.decide(D("99"), sma, 1, now + timedelta(minutes=21)) == "sell"   # 보유 시간 초과
    assert st.decide(D("100"), sma, 1, now) == "sell"             # 평균 복귀


def test_bar_check_compares_realtime_with_rest() -> None:
    bc = BarCheck()
    t0 = datetime(2026, 10, 14, 9, 1, tzinfo=KST)
    rest = []
    for i in range(4):
        ts = t0 + timedelta(minutes=i)
        rest.append({"ts": ts.isoformat(), "close": "100", "volume": 10})
        if i != 2:
            bc.add({"close": "101" if i == 3 else "100", "volume": 10}, ts)
    r = bc.compare(rest, t0, t0 + timedelta(minutes=4))
    assert (r["rest_bars"], r["realtime_bars"], r["missing"], r["close_mismatch"]) == (4, 3, ["09:03"], ["09:04"])


def test_pilot_order_events_out_of_order(tmp_path: Path) -> None:
    p = Pilot(None, RecNotifier(), tmp_path)  # type: ignore[arg-type]
    base = {"id": 7, "symbol": "KR:005930", "side": "buy", "qty": 1, "order_type": "market"}
    p.on_order({**base, "status": "FILLED", "filled_qty": 1})     # WS 이벤트가 먼저
    p.on_order({**base, "status": "ACCEPTED", "filled_qty": 0})   # 늦게 온 REST 응답
    assert p.positions == {"KR:005930": 1} and p.orders == {}
    p.on_order({**base, "id": 8, "side": "sell", "status": "PARTIALLY_FILLED", "filled_qty": 1, "qty": 1})
    p.on_order({**base, "id": 8, "side": "sell", "status": "FILLED", "filled_qty": 1, "qty": 1})
    assert p.positions == {"KR:005930": 0}


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 작업 스케줄러 XML")
def test_task_xml_escapes_and_has_restart_trigger() -> None:
    from central.ops.winservice import task_xml

    xml = task_xml(r"E:\a&b\python.exe", Path(r"E:\repo"), r"PC\user")
    assert "a&amp;b" in xml and "<LogonType>S4U</LogonType>" in xml and "<Interval>PT5M</Interval>" in xml
    assert "IgnoreNew" in xml and "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in xml


def test_market_hours_trading_day() -> None:
    h = MarketHours(holidays={"KR": {date(2026, 10, 9)}, "US": set()})
    assert h.is_trading_day("KR", date(2026, 10, 8)) and not h.is_trading_day("KR", date(2026, 10, 9))
    assert not h.is_trading_day("US", date(2026, 10, 10))
