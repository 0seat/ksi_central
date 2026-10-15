"""운영 작업: 일정 작업 등록, 연결/시세 감시, 이벤트 알림, 백업, 일일 보고.

일정 (현지 시각, 개장일 조건은 휴장일 캘린더 기준)
    07:00 KST 매일        maintenance   DB 백업, 지난 날짜 틱 파일 합치기, 오래된 백업 정리, 디스크/USD 재고 점검
    07:20 KST 매일        reference     국내 휴장일 갱신, 평일이면 종목 마스터 갱신
    08:40 KST 국내 개장일 kr_preopen    장 시작 전 점검 (연결, 정지, 거래 스위치)
    15:45 KST 국내 개장일 kr_close      주문 동기화, 대사, 일일 보고
    20:10 KST 국내 개장일 kr_expire     동기화 후 남은 미체결 소멸 처리 (NXT 애프터마켓 20:00 종료 뒤)
    16:20 ET  미국 개장일 us_close      주문 동기화, 미체결 소멸, 대사
    16:10 KST 국내 개장일 history_kr    과거 시세 보관 증분 (대상은 central/marketdata/history_targets.py)
    17:00 ET  미국 개장일 history_us    과거 시세 보관 증분
과거 시세 처음 받기 (백그라운드)
    서버 시작, 앱 등록, 나스닥100 구성 변경 때 아직 보관본이 없는 대상을 끝까지 받는다 (종목당 수 분~15분).
    그 시장 장중이면 미루고, 장 마감 후 history_kr/history_us 가 다시 부른다
감시 (30초마다)
    실시간 세션이 ws_down_alert_seconds 넘게 끊기면 알림, 복구되면 알림
    장중에 상시 수집(watchlist) 종목 체결이 stale_tick_minutes 넘게 없으면 알림
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sqlite3
import zipfile
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from central.core.events import Event
from central.core.market_hours import KST, NEW_YORK
from central.ops.notify import Notifier
from central.ops.scheduler import Job, Scheduler
from central.storage.models import FillRow, OrderRow, SubAccountRow

if TYPE_CHECKING:
    from central.app import CentralApp

log = logging.getLogger(__name__)

_KR_WATCH = (time(9, 5), time(15, 15))
_US_WATCH_START = time(9, 40)


class Operations:
    def __init__(self, app: CentralApp, notifier: Notifier) -> None:
        self.app, self.notifier = app, notifier
        self.cfg = app.settings.ops
        self.scheduler = Scheduler(app.db, clock=app.clock,
                                   on_failure=lambda name, d: notifier.error(f"작업 실패: {name}", d))
        self._down_since: dict[str, datetime] = {}
        self._down_alerted: set[str] = set()
        self._stale: set[str] = set()
        self.started_at: datetime | None = None
        self.last_backup: Path | None = None
        from central.marketdata.history_cli import history_root
        from central.marketdata.history_targets import HistoryTargets
        self.history_targets = HistoryTargets(app.db, history_root(app.settings), app.settings.history,
                                              clock=app.clock)
        self._backfill_task: asyncio.Task | None = None
        self._backfill_again = False
        self._backfilling: str | None = None
        self.backfill_errors: dict[str, str] = {}
        self._register_jobs()
        self.scheduler.every("watchdog", 30, self.watch)
        app.bus.subscribe(self.on_event)
        app.market.listeners.append(self.on_market)

    # ───────────── 등록 ─────────────
    def _accounts(self, market: str) -> list[str]:
        return [a for a, p in self.app.profiles.items() if market in p.markets]

    def _register_jobs(self) -> None:
        hours = self.app.hours
        kr_day = lambda d: hours.is_trading_day("KR", d)  # noqa: E731
        us_day = lambda d: hours.is_trading_day("US", d)  # noqa: E731
        add = self.scheduler.add
        add(Job("maintenance", time(7, 0), self.maintenance, description="DB 백업, 틱 합치기, 디스크/USD 점검"))
        add(Job("reference", time(7, 20), self.reference, description="휴장일, 종목 마스터 갱신"))
        if self._accounts("KR"):
            add(Job("kr_preopen", time(8, 40), self.kr_preopen, when=kr_day, until=time(9, 0),
                    description="국내 장 시작 전 점검"))
            add(Job("kr_close", time(15, 45), self.kr_close, when=kr_day, description="국내 동기화, 대사, 일일 보고"))
            add(Job("kr_expire", time(20, 10), self.kr_expire, when=kr_day, description="국내 미체결 소멸 처리"))
        if self._accounts("US"):
            add(Job("us_close", time(16, 20), self.us_close, tz=NEW_YORK, when=us_day,
                    description="미국 동기화, 미체결 소멸, 대사"))
        # 과거 시세 보관: 대상은 앱이 API 로 바꿀 수 있어 실행할 때마다 다시 읽는다
        add(Job("history_kr", time(16, 10), lambda: self.history("KR"), when=kr_day,
                description="국내 과거 시세 보관 (1분봉, 일봉, 기업행위)"))
        add(Job("history_us", time(17, 0), lambda: self.history("US"), tz=NEW_YORK, when=us_day,
                description="미국 과거 시세 보관 (1분봉, 일봉, 기업행위)"))
        self.scheduler.every("history_index", self.app.settings.history.index_minutes_every * 60,
                             self.history_index_minutes)

    async def start(self) -> None:
        self.started_at = self.app.clock()
        await self.scheduler.start()
        self.history_backfill_soon()
        acc = ", ".join(f"{a}({p.env}, 거래 {'켬' if p.trading_enabled else '끔'})" for a, p in self.app.profiles.items())
        halted = self.app.halted()
        body = f"계좌: {acc}\n시세 수신: {self.app.market.stats()['sources']}"
        if halted:
            body += f"\n정지 유지 중: {halted}"
        self.notifier.info("서버 시작", body)

    async def stop(self) -> None:
        await self.scheduler.stop()
        if self._backfill_task and not self._backfill_task.done():
            self._backfill_task.cancel()
            try:
                await self._backfill_task
            except asyncio.CancelledError:
                pass
        self.notifier.info("서버 종료", "정상 종료")

    # ───────────── 이벤트 알림 ─────────────
    def on_event(self, e: Event) -> None:
        p = e.payload
        if e.type == "RECONCILE" and not p.get("ok"):
            halted = ("broker_market", f"{p['broker_account_id']}:{p['market']}") in self.app.risk.halted
            body = "\n".join(f"- {d}" for d in p.get("diffs", []))
            if halted:
                body += "\n해당 실계좌 x 시장 신규 주문 정지됨. 확인 후 관리자 API 로 보정하고 해제할 것"
            self.notifier.error(f"대사 불일치 {p['broker_account_id']}/{p['market']}", body)
        elif e.type == "UNMATCHED_FILL":
            self.notifier.warning("매핑 없는 체결 (미배정으로 귀속)", _kv(p))
        elif e.type == "ORDER_UNKNOWN":
            self.notifier.warning(f"접수 불확실 주문 {p.get('id')}", _kv(p, ("id", "symbol", "side", "qty", "price")),
                                  key=f"unknown:{p.get('id')}")
        elif e.type == "ORDER_AMBIGUOUS":
            self.notifier.error(f"주문 {p.get('order_id')} 수동 확인 필요", _kv(p))
        elif e.type == "HALT":
            self.notifier.warning(f"긴급 정지 {p['scope']} {p['id']}".strip(), key=f"halt:{p['scope']}:{p['id']}")
        elif e.type == "RESUME":
            self.notifier.info(f"정지 해제 {p['scope']} {p['id']}".strip(), key=f"resume:{p['scope']}:{p['id']}")
        elif e.type == "CASH_RESOLVED":
            self.notifier.warning(f"대사 현금 차이 자동 처리 {p['broker_account_id']}/{p['market']}",
                                  "\n".join(f"- {x}" for x in p["lines"]))
        elif e.type in ("DEPOSIT", "WITHDRAW"):
            to = ""
            if p.get("sub_account_id"):
                to = f" ({'→' if e.type == 'DEPOSIT' else '←'} 서브계좌 {p['sub_account_id']})"
            self.notifier.info(f"{'입금' if e.type == 'DEPOSIT' else '출금'} 반영 {p['broker_account_id']} "
                               f"{p['currency']} {p['amount']}{to}", p.get("memo") or "")
        elif e.type == "TRADING_ENABLED":
            self.notifier.info(f"거래 스위치 {p['broker_account_id']} {'켬' if p['enabled'] else '끔'}",
                               key=f"trading:{p['broker_account_id']}:{p['enabled']}")

    def on_market(self, channel: str, item: object) -> None:
        if channel.startswith("dropped:"):
            self.notifier.error(f"시세 채널 중단 {channel.removeprefix('dropped:')}", str(item))

    # ───────────── 과거 시세 보관 ─────────────
    def _history_fetcher(self):
        from central.marketdata.history import HistoryFetcher, HistoryStore
        from central.marketdata.history_cli import _account, history_root

        settings = self.app.settings
        return HistoryFetcher(HistoryStore(history_root(settings)),
                              lambda m: self.app.brokers[_account(settings, m)], clock=self.app.clock)

    def _history_store(self):
        from central.marketdata.history import HistoryStore
        return HistoryStore(self.history_targets.root)

    def _needs_backfill(self, key) -> bool:
        """보관본이 아직 없으면 True (일봉이 없거나, 종목인데 분봉이 없음)."""
        store = self._history_store()
        if not (store.root / key.folder / "1d.parquet").exists():
            return True
        return not key.index and store.last_minute(key) is None

    async def _history_one(self, fetcher, sym: str) -> tuple[str, list[str]]:
        """한 대상을 받는다. 종목은 일봉(이벤트)을 먼저: 분봉을 원주가로 되돌리는 데 쓴다. (요약, 새 기업행위)."""
        from central.marketdata.history import SeriesKey

        key = SeriesKey.parse(sym)
        if key.index:
            d = await fetcher.index_daily(key)
            m = await fetcher.index_minutes(key)
            return f"{sym} 분봉 +{m['added']} 일봉 +{d['added']}", []
        inst = self.app.instruments.get(sym) or self.app.instruments.ensure(sym)
        before = len(fetcher.store.events(key))
        d = await fetcher.daily(inst)
        m = await fetcher.minutes(inst)
        new = []
        if before and d["events"] > before:   # 처음 받을 때 찾은 과거 이벤트는 알리지 않는다
            new = [f"{sym} {e.date} x{e.ratio} ({e.kind})" for e in fetcher.store.events(key)[before:]]
        return f"{sym} 분봉 +{m['added']} 일봉 +{d['added']}", new

    async def history(self, market: str) -> str:
        """장 마감 후 증분: 종목 1분봉/일봉/기업행위, 지수 일봉/1분봉. 새 기업행위가 생기면 알린다.
        보관본이 아직 없는 대상은 백그라운드 처음 받기(history_backfill_soon)가 맡는다."""
        from central.marketdata.history import SeriesKey

        targets = self.history_targets.symbols(market) + self.history_targets.indices(market)
        if not targets:
            return "대상 없음"
        fetcher = self._history_fetcher()
        failed, new_events, lines, pending = [], [], [], 0
        for sym in targets:
            if sym == self._backfilling or self._needs_backfill(SeriesKey.parse(sym)):
                pending += 1
                continue
            try:
                line, new = await self._history_one(fetcher, sym)
                lines.append(line)
                new_events += new
            except Exception as e:
                failed.append(f"{sym}: {type(e).__name__}: {e}")
        if pending:
            self.history_backfill_soon()
        log.info("과거 시세 보관 %s: %s", market, "; ".join(lines))
        if new_events:
            self.notifier.warning(f"기업행위 감지 ({market})", "\n".join(new_events))
        if failed:
            raise RuntimeError(f"과거 시세 보관 {len(failed)}/{len(targets)} 실패:\n" + "\n".join(failed))
        return f"{len(lines)}개 갱신" + (f", 처음 받기 대기 {pending}개" if pending else "")

    def history_backfill_soon(self) -> None:
        """보관본이 없는 대상을 백그라운드로 처음부터 받는다. 이미 돌고 있으면 끝난 뒤 한 번 더 훑는다."""
        if self._backfill_task is None or self._backfill_task.done():
            self._backfill_task = asyncio.create_task(self._history_backfill(), name="history-backfill")
        else:
            self._backfill_again = True

    async def _history_backfill(self) -> None:
        from central.marketdata.history import SeriesKey

        while True:
            self._backfill_again = False
            todo = [k for k in self.history_targets.sources() if self._needs_backfill(SeriesKey.parse(k))]
            done, failed = [], []
            if todo:
                log.info("과거 시세 처음 받기 %d개 시작: %s", len(todo), ", ".join(todo))
                fetcher = self._history_fetcher()
            deferred = 0
            for sym in todo:
                if sym not in self.history_targets.sources():   # 그사이 등록 취소
                    continue
                if self._session_open(SeriesKey.parse(sym).market):   # 대량 백필은 장이 닫힌 뒤 (PLAN 7.3)
                    deferred += 1
                    continue
                self._backfilling = sym
                try:
                    line, _ = await self._history_one(fetcher, sym)
                    done.append(sym)
                    self.backfill_errors.pop(sym, None)
                    log.info("과거 시세 처음 받기 %s", line)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self.backfill_errors[sym] = f"{type(e).__name__}: {e}"[:300]
                    failed.append(f"{sym}: {self.backfill_errors[sym]}")
                    log.warning("과거 시세 처음 받기 실패 %s", failed[-1])
                finally:
                    self._backfilling = None
            if done:
                self.notifier.info(f"과거 시세 처음 받기 {len(done)}개 끝", ", ".join(done)[:1500])
            if failed:
                self.notifier.warning(f"과거 시세 처음 받기 {len(failed)}개 실패", "\n".join(failed)[:1500])
            if deferred:
                log.info("과거 시세 처음 받기 %d개는 장 마감 후 일정 작업 때 이어서", deferred)
            if not self._backfill_again:
                return

    def _session_open(self, market: str) -> bool:
        """정규장 앞뒤 여유를 둔 장중 여부. 이때는 처음 받기를 미루고 장 마감 후 일정 작업이 다시 부른다."""
        now = self.app.clock().astimezone(KST if market == "KR" else NEW_YORK)
        start, end = (time(8, 30), time(15, 40)) if market == "KR" else (time(9, 0), time(16, 10))
        return self.app.hours.is_trading_day(market, now.date()) and start <= now.time() <= end

    def history_status(self) -> list[dict]:
        """대상별 출처와 보관 상태 (API 용). state: ok 보관 중, fetching 처음 받는 중,
        pending 처음 받기 대기 (장중에 등록하면 그 시장 마감 후)."""
        from central.marketdata.history import SeriesKey

        store = self._history_store()
        out = []
        for sym, sources in self.history_targets.sources().items():
            key = SeriesKey.parse(sym)
            state = ("fetching" if sym == self._backfilling else "pending" if self._needs_backfill(key) else "ok")
            out.append({"symbol": sym, "sources": sources, "state": state,
                        "last_minute": None if key.index else store.last_minute(key),
                        "error": self.backfill_errors.get(sym)})
        return out

    async def history_index_minutes(self) -> None:
        """장중 지수 1분봉: KIS 가 최근 약 100개만 주므로 주기적으로 받아 쌓는다."""
        from central.marketdata.history import SeriesKey

        now = self.app.clock()
        fetcher = None
        for sym in self.history_targets.indices():
            key = SeriesKey.parse(sym)
            if self._needs_backfill(key):   # 일봉부터 받는 처음 받기에 맡긴다
                continue
            local = now.astimezone(key.zone)
            hours = (time(9, 0), time(15, 45)) if key.market == "KR" else (time(9, 30), time(16, 5))
            if not (self.app.hours.is_trading_day(key.market, local.date()) and hours[0] <= local.time() <= hours[1]):
                continue
            fetcher = fetcher or self._history_fetcher()
            try:
                await fetcher.index_minutes(key)
            except Exception as e:
                log.warning("지수 분봉 수집 실패 %s: %s", sym, e)

    # ───────────── 감시 ─────────────
    async def watch(self) -> None:
        now = self.app.clock()
        for alias, broker in self.app.brokers.items():
            connected = broker.health().get("connected", True)
            if not connected:
                since = self._down_since.setdefault(alias, now)
                if alias not in self._down_alerted and (now - since).total_seconds() >= self.cfg.ws_down_alert_seconds:
                    self._down_alerted.add(alias)
                    last = (broker.health().get("realtime") or {}).get("last_error")
                    self.notifier.error(f"실시간 세션 끊김 {alias}",
                                        f"{since:%H:%M:%S} 부터. 체결통보와 시세 수신 중단, 재연결 시도 중\n{last or ''}",
                                        key=f"ws_down:{alias}")
            elif alias in self._down_since:
                since = self._down_since.pop(alias)
                if alias in self._down_alerted:
                    self._down_alerted.discard(alias)
                    mins = (now - since).total_seconds() / 60
                    self.notifier.info(f"실시간 세션 복구 {alias}", f"끊김 {mins:.1f}분. 재연결 후 주문 동기화 실행",
                                       key=f"ws_up:{alias}")
        self._watch_ticks(now)

    def _in_watch_window(self, market: str, now: datetime) -> bool:
        hours = self.app.hours
        if market == "KR":
            local = now.astimezone(KST)
            return hours.is_trading_day("KR", local.date()) and _KR_WATCH[0] <= local.time() < _KR_WATCH[1]
        local = now.astimezone(NEW_YORK)
        if not hours.is_trading_day("US", local.date()):
            return False
        close = hours.us_early_close.get(local.date(), time(16, 0))
        end = (datetime.combine(local.date(), close) - timedelta(minutes=10)).time()
        return _US_WATCH_START <= local.time() < end

    def _watch_ticks(self, now: datetime) -> None:
        limit = timedelta(minutes=self.cfg.stale_tick_minutes)
        for symbol in self.app.settings.market_data.watchlist:
            symbol = symbol.upper()
            last = self.app.market.last_tick.get(symbol)
            fresh = last is not None and now - last.ts <= limit
            if fresh and symbol in self._stale:
                self._stale.discard(symbol)
                self.notifier.info(f"시세 수신 재개 {symbol}", key=f"tick_up:{symbol}")
            if fresh or not self._in_watch_window(symbol[:2], now):
                continue
            ref = last.ts if last else self.started_at
            if ref is not None and now - ref > limit and symbol not in self._stale:
                self._stale.add(symbol)
                self.notifier.error(f"시세 수신 멈춤 {symbol}",
                                    f"마지막 체결 {last.ts:%H:%M:%S}" if last else "시작 후 체결 수신 없음",
                                    key=f"tick_down:{symbol}")

    # ───────────── 일정 작업 ─────────────
    async def maintenance(self) -> str:
        app = self.app
        today = app.clock().date()
        parts = []
        path = await asyncio.to_thread(self.backup_db)
        parts.append(f"백업 {path.name} ({path.stat().st_size / 1e6:.1f}MB)")
        if app.ticks is not None:
            app.ticks.flush()
            n = await asyncio.to_thread(app.ticks.compact_before, today)
            parts.append(f"틱 합치기 {n}종목")
        free_gb = shutil.disk_usage(app.settings.home).free / 1e9
        parts.append(f"디스크 여유 {free_gb:.0f}GB")
        if free_gb < self.cfg.disk_min_free_gb:
            self.notifier.error("디스크 여유 부족", f"{app.settings.home} 여유 {free_gb:.1f}GB (기준 {self.cfg.disk_min_free_gb}GB)")
        for alias, p in app.profiles.items():
            if p.usd_inventory_min is None or "US" not in p.markets:
                continue
            usd = app.accounts.balances(app.accounts.unallocated(alias).id).get("USD")
            have = usd.available if usd else Decimal("0")
            parts.append(f"{alias} 미배정 USD {have}")
            if have < p.usd_inventory_min:
                self.notifier.warning(f"USD 재고 부족 {alias}",
                                      f"미배정 USD {have} < 최소 {p.usd_inventory_min}. 목표 {p.usd_inventory_target} 까지 환전 후 "
                                      f"POST /admin/fx 로 반영", key=f"usd:{alias}")
        return ", ".join(parts)

    def backup_db(self) -> Path:
        """SQLite 온라인 백업 → zip. backup_keep 개만 남긴다."""
        src = self.app.settings.data_dir / "central.db"
        out_dir = self.app.settings.backups_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = self.app.clock().strftime("%Y%m%d-%H%M%S")
        tmp = out_dir / f"central-{stamp}.db"
        s, d = sqlite3.connect(src), sqlite3.connect(tmp)
        try:
            s.backup(d)
        finally:
            d.close()
            s.close()
        target = out_dir / f"central-{stamp}.db.zip"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(tmp, tmp.name)
        tmp.unlink()
        for old in sorted(out_dir.glob("central-*.db.zip"))[:-self.cfg.backup_keep]:
            old.unlink()
        self.last_backup = target
        return target

    async def reference(self) -> str:
        app = self.app
        n = await app.calendar.refresh_kr()
        app.calendar.apply()
        parts = [f"휴장일 {n}일 갱신" if n else "휴장일 오늘 이미 받음"]
        if app.clock().weekday() < 5 or not app.catalog.has_master():
            parts.append(f"종목 마스터 {await app.refresh_master()}")
        if app.settings.history.nasdaq100:
            try:
                ch = await self.history_targets.refresh_nasdaq100(app.http)
                parts.append(f"나스닥100 {ch['count']}종목")
                if ch["added"] or ch["removed"]:
                    parts.append(f"편입 {ch['added']} 편출 {ch['removed']}")
                    self.notifier.info("나스닥100 구성 변경", f"편입 {ch['added']}\n편출 {ch['removed']} (보관본은 남김)")
                    self.history_backfill_soon()
            except Exception as e:   # 실패하면 지난 목록을 계속 쓴다
                parts.append(f"나스닥100 갱신 실패 {type(e).__name__}: {e}")
        return ", ".join(parts)

    async def kr_preopen(self) -> str:
        app = self.app
        lines = []
        for alias in self._accounts("KR"):
            h = app.brokers[alias].health()
            rt = h.get("realtime") or {}
            lines.append(f"{alias}: 연결 {'정상' if h.get('connected') else '끊김'}, "
                         f"거래 {'켬' if app.profiles[alias].trading_enabled else '끔'}, "
                         f"실시간 등록 {rt.get('slots_used', '-')}/{rt.get('slots_limit', '-')}")
        halted = app.halted()
        lines.append(f"정지: {halted or '없음'}")
        body = "\n".join(lines)
        level = "warning" if halted or any("끊김" in x for x in lines) else "info"
        self.notifier.notify(level, "국내 장 시작 전 점검", body)
        return body.replace("\n", " / ")

    async def _sync_and_reconcile(self, market: str, *, expire: bool) -> list[str]:
        app = self.app
        out = []
        for alias in self._accounts(market):
            rep = await app.sync.run(alias, market)
            line = f"{alias}/{market}"
            if rep.synthetic_fills or rep.cancels or rep.failed or rep.resolved:
                line += f" 동기화(체결 보정 {rep.synthetic_fills}, 취소 {rep.cancels}, 확정 {len(rep.resolved)}, 실패 {len(rep.failed)})"
            if expire:
                n = app.orders.expire_open_orders(alias, market)
                if n:
                    line += f" 미체결 소멸 {n}건"
            res = await app.reconciler.run(alias, market, halt_on_mismatch=self.cfg.halt_on_reconcile_mismatch)
            line += " 대사 일치" if res.ok else f" 대사 불일치 {len(res.diffs)}건"
            if not res.cash_checked:
                line += "(현금 제외)"
            out.append(line)
        return out

    async def kr_close(self) -> str:
        lines = await self._sync_and_reconcile("KR", expire=False)
        if self.app.ticks:
            self.app.ticks.flush()
        report = self.daily_report(self.app.clock().astimezone(KST).date(), "KR", lines)
        self.notifier.info("일일 보고 (국내 장 마감)", report, key=f"report:KR:{self.app.clock():%Y%m%d}")
        return " / ".join(lines)

    async def kr_expire(self) -> str:
        app = self.app
        out = []
        for alias in self._accounts("KR"):
            await app.sync.run(alias, "KR")
            n = app.orders.expire_open_orders(alias, "KR")
            out.append(f"{alias} 소멸 {n}건")
        return ", ".join(out)

    async def us_close(self) -> str:
        lines = await self._sync_and_reconcile("US", expire=True)
        day = self.app.clock().astimezone(NEW_YORK).date()
        self.notifier.info("미국 장 마감", "\n".join(lines) + "\n" + self._activity(day, "US"),
                           key=f"report:US:{day}")
        return " / ".join(lines)

    # ───────────── 보고 ─────────────
    def _activity(self, day: date, market: str) -> str:
        """서브계좌별 그날 주문/체결/실현손익 (KST 날짜 기준, 미국은 ET 날짜의 KST 범위)."""
        tz = KST if market == "KR" else NEW_YORK
        start = datetime.combine(day, time.min, tz).astimezone(KST)
        end = start + timedelta(days=1)
        with self.app.db.session() as s:
            names = dict(s.execute(select(SubAccountRow.id, SubAccountRow.name)).all())
            orders = s.execute(select(OrderRow.sub_account_id, OrderRow.status, func.count()).where(
                OrderRow.market == market, OrderRow.created_at >= start, OrderRow.created_at < end)
                .group_by(OrderRow.sub_account_id, OrderRow.status)).all()
            fills = s.execute(select(FillRow.sub_account_id, FillRow.qty, FillRow.realized_pnl, FillRow.fee, FillRow.tax)
                              .where(FillRow.instrument_id.like(f"{market}:%"), FillRow.filled_at >= start,
                                     FillRow.filled_at < end)).all()
        by_sub: dict[int, dict] = {}
        for sid, status, n in orders:
            d = by_sub.setdefault(sid, {"orders": {}, "fills": 0, "qty": 0, "pnl": Decimal(0), "cost": Decimal(0)})
            d["orders"][status] = n
        for sid, qty, pnl, fee, tax in fills:
            d = by_sub.setdefault(sid, {"orders": {}, "fills": 0, "qty": 0, "pnl": Decimal(0), "cost": Decimal(0)})
            d["fills"] += 1
            d["qty"] += qty
            d["pnl"] += pnl
            d["cost"] += fee + tax
        if not by_sub:
            return "주문/체결 없음"
        lines = []
        for sid, d in sorted(by_sub.items()):
            total = sum(d["orders"].values())
            st = ", ".join(f"{k} {v}" for k, v in sorted(d["orders"].items()))
            lines.append(f"- {names.get(sid, sid)}: 주문 {total} ({st}), 체결 {d['fills']}건 {d['qty']}주, "
                         f"실현손익 {d['pnl']:,} (수수료/세금 {d['cost']:,})")
        return "\n".join(lines)

    def daily_report(self, day: date, market: str, recon_lines: list[str]) -> str:
        app = self.app
        lines = ["**대사**"] + [f"- {x}" for x in recon_lines]
        lines += ["**서브계좌 활동**", self._activity(day, market)]
        lines.append("**시세**")
        if app.ticks:
            counts = app.ticks.day_counts(day)
            lines.append("- 틱 수신: " + (", ".join(f"{k} {v:,}" for k, v in counts.items()) or "없음"))
        sessions = []
        for alias, b in app.brokers.items():
            rt = b.health().get("realtime")
            if rt:
                sessions.append(f"{alias} 연결 {rt['connects']}회, 등록 {rt['slots_used']}/{rt['slots_limit']}")
        if sessions:
            lines.append("- 실시간 세션 (서버 시작 이후): " + "; ".join(sessions))
        lines.append(f"**정지**: {app.halted() or '없음'}")
        free_gb = shutil.disk_usage(app.settings.home).free / 1e9
        backup = self.last_backup.name if self.last_backup else "이번 실행에서 아직 없음"
        lines.append(f"**시스템**: 디스크 여유 {free_gb:.0f}GB, 마지막 백업 {backup}, "
                     f"서버 시작 {self.started_at:%m-%d %H:%M}" if self.started_at else f"**시스템**: 디스크 여유 {free_gb:.0f}GB")
        return "\n".join(lines)

    def status(self) -> dict:
        return {"jobs": self.scheduler.status(), "ws_down": {a: t for a, t in self._down_since.items()},
                "stale_symbols": sorted(self._stale),
                "notifier": {"enabled": self.notifier.enabled, "sent": self.notifier.sent,
                             "failed": self.notifier.failed, "dropped": self.notifier.dropped}}


def _kv(p: dict, keys: tuple[str, ...] | None = None) -> str:
    items = ((k, p[k]) for k in (keys or p.keys()) if k in p)
    return "\n".join(f"{k}: {v}" for k, v in items)
