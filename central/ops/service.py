"""운영 작업: 일정 작업 등록, 연결/시세 감시, 이벤트 알림, 백업, 일일 보고.

일정 (현지 시각, 개장일 조건은 휴장일 캘린더 기준)
    07:00 KST 매일        maintenance   DB 백업, 지난 날짜 틱 파일 합치기, 오래된 백업 정리, 디스크/USD 재고 점검
    07:20 KST 매일        reference     국내 휴장일 갱신, 평일이면 종목 마스터 갱신
    08:40 KST 국내 개장일 kr_preopen    장 시작 전 점검 (연결, 정지, 거래 스위치)
    15:45 KST 국내 개장일 kr_close      주문 동기화, 대사, 일일 보고
    20:10 KST 국내 개장일 kr_expire     동기화 후 남은 미체결 소멸 처리 (NXT 애프터마켓 20:00 종료 뒤)
    16:20 ET  미국 개장일 us_close      주문 동기화, 미체결 소멸, 대사
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

    async def start(self) -> None:
        self.started_at = self.app.clock()
        await self.scheduler.start()
        acc = ", ".join(f"{a}({p.env}, 거래 {'켬' if p.trading_enabled else '끔'})" for a, p in self.app.profiles.items())
        halted = self.app.halted()
        body = f"계좌: {acc}\n시세 수신: {self.app.market.stats()['sources']}"
        if halted:
            body += f"\n정지 유지 중: {halted}"
        self.notifier.info("서버 시작", body)

    async def stop(self) -> None:
        await self.scheduler.stop()
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
        elif e.type == "TRADING_ENABLED":
            self.notifier.info(f"거래 스위치 {p['broker_account_id']} {'켬' if p['enabled'] else '끔'}",
                               key=f"trading:{p['broker_account_id']}:{p['enabled']}")

    def on_market(self, channel: str, item: object) -> None:
        if channel.startswith("dropped:"):
            self.notifier.error(f"시세 채널 중단 {channel.removeprefix('dropped:')}", str(item))

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
