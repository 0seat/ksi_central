"""파일럿 실행부: 스트림 수신, 매매, 시험 주문, 자체 점검, 일일 보고.

중앙증권과 별도 프로세스이며 서브계좌 API 키와 SDK(central.client)만 쓴다.
서버가 재시작되거나 연결이 끊겨도 스스로 다시 붙고, 붙을 때마다 REST 로 상태를 다시 맞춘다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time as _time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Callable

from apps.pilot.strategy import BarCheck, SignalState
from central.client import CentralClient, CentralError
from central.core.market_hours import KST, now_kst
from central.ops.notify import Notifier

log = logging.getLogger("pilot")

SYMBOLS = ("KR:005930", "KR:000660")     # 매매
WATCH_ONLY = ("US:AAPL",)                 # 시세만 (미국 실시간 수신 확인)
TERMINAL = {"FILLED", "CANCELED", "REJECTED", "SUBMIT_FAILED", "EXPIRED", "REPLACED"}
TRADE_START, TRADE_END, FLATTEN_AT, REPORT_AT = time(9, 5), time(15, 10), time(15, 10), time(15, 40)
PROBE_EVERY = timedelta(minutes=30)
CHECK_EVERY = timedelta(minutes=5)
STUCK_AFTER = timedelta(minutes=3)


@dataclass
class DayStats:
    day: date
    stream_connects: int = 0
    stream_drops: int = 0
    messages: Counter = field(default_factory=Counter)
    ticks: Counter = field(default_factory=Counter)
    orders: Counter = field(default_factory=Counter)        # 종류별 접수 시도
    rejects: list[str] = field(default_factory=list)
    probes_ok: int = 0
    probes_failed: list[str] = field(default_factory=list)
    check_failures: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    latency_ms: list[float] = field(default_factory=list)   # 주문 요청 → 응답
    flattened: bool = False
    reported: bool = False


class Pilot:
    def __init__(self, cs: CentralClient, notifier: Notifier, state_dir: Path, *,
                 clock: Callable[[], datetime] = now_kst, stop_file: Path | None = None) -> None:
        self.cs, self.notifier, self.state_dir, self.clock, self.stop_file = cs, notifier, state_dir, clock, stop_file
        self.stats = DayStats(clock().date())
        self.positions: dict[str, int] = {}
        self.orders: dict[int, dict] = {}            # 진행 중 주문 (id → 마지막 상태)
        self.order_seen_at: dict[int, datetime] = {}
        self.filled: dict[int, int] = {}             # 주문별 반영한 체결 수량
        self.done: set[int] = set()                  # 끝난 주문
        self.signals = {s: SignalState() for s in SYMBOLS}
        self.bars = {s: BarCheck() for s in SYMBOLS}
        self.trading_day: bool | None = None
        self.last_probe: datetime | None = None
        self.last_check: datetime | None = None
        self.connected = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()

    # ───────────── 시작 ─────────────
    async def run(self) -> None:
        log.info("파일럿 시작")
        await self._wait_server()
        me = await self.cs.me()
        self.notifier.info("파일럿 시작", f"서브계좌 {me['name']}(#{me['id']}) 매매 {SYMBOLS}, 시세 {WATCH_ONLY}")
        await self.resync()
        await self.refresh_day()
        await self.seed()
        loops = [asyncio.create_task(self.stream_loop(), name="stream"),
                 asyncio.create_task(self.clock_loop(), name="clock")]
        try:
            await self._wait_stop()
        finally:
            for t in loops + list(self._tasks):
                t.cancel()
            await asyncio.gather(*loops, *self._tasks, return_exceptions=True)
            self.save_state()
            self.notifier.info("파일럿 종료", "종료 요청")
            log.info("파일럿 종료")

    async def _wait_server(self) -> None:
        delay = 2.0
        while True:
            try:
                await self.cs.me()
                return
            except (CentralError, OSError, Exception) as e:   # 서버 기동 전
                log.warning("서버 대기 (%s)", type(e).__name__)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    async def _wait_stop(self) -> None:
        while not (self.stop_file and self.stop_file.exists()):
            await asyncio.sleep(1)
        self.stop_file.unlink(missing_ok=True)

    def spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._task_done)

    def _task_done(self, t: asyncio.Task) -> None:
        self._tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            self.error(f"작업 실패: {type(t.exception()).__name__}: {t.exception()}")

    def error(self, msg: str, *, notify: bool = True) -> None:
        log.error(msg)
        self.stats.errors.append(f"{self.clock():%H:%M:%S} {msg}")
        if notify:
            self.notifier.error("파일럿 이상", msg, key=f"err:{msg[:40]}")

    # ───────────── 상태 동기화 ─────────────
    async def resync(self) -> None:
        """REST 로 보유와 진행 중 주문을 다시 맞춘다 (시작, 재접속 때)."""
        self.positions = {p["symbol"]: p["qty"] for p in await self.cs.positions("KR")}
        today = self.clock().date()
        open_orders = [o for o in await self.cs.orders(start=today, limit=500) if o["status"] not in TERMINAL]
        self.orders = {o["id"]: o for o in open_orders}
        self.filled.update({o["id"]: o["filled_qty"] for o in open_orders})
        now = self.clock()
        self.order_seen_at = {o["id"]: now for o in open_orders}
        log.info("상태 동기화: 보유 %s, 진행 중 주문 %d", self.positions, len(self.orders))

    async def refresh_day(self) -> None:
        today = self.clock().date()
        try:
            days = (await self.cs.calendar("KR", start=today, end=today))["days"]
            self.trading_day = bool(days and days[0]["open"])
        except CentralError as e:
            self.error(f"캘린더 조회 실패 {e.code}", notify=False)
            self.trading_day = today.weekday() < 5
        log.info("%s 국내 개장일=%s", today, self.trading_day)

    async def seed(self) -> None:
        """이동평균 초기값: 오늘(없으면 최근) 1분봉 종가."""
        for sym in SYMBOLS:
            try:
                res = await self.cs.candles(sym, "1m", limit=self.signals[sym].window)
            except CentralError as e:
                self.error(f"{sym} 1분봉 조회 실패 {e.code} {e.message}", notify=False)
                continue
            for b in res["candles"]:
                self.signals[sym].push(Decimal(b["close"]))
            log.info("%s 이동평균 초기값 %d봉", sym, len(self.signals[sym].closes))

    # ───────────── 스트림 ─────────────
    async def stream_loop(self) -> None:
        channels = [f"{k}:{s}" for s in SYMBOLS for k in ("tick", "candle.1m")]
        channels += [f"orderbook:{SYMBOLS[0]}"] + [f"tick:{s}" for s in WATCH_ONLY]
        delay = 1.0
        first = True
        while True:
            started = _time.monotonic()
            try:
                async for msg in self.cs.stream(channels):
                    if msg["ch"] == "system" and msg["data"].get("type") == "WELCOME":
                        self.stats.stream_connects += 1
                        self.connected.set()
                        if not first:
                            await self.resync()
                        first = False
                        continue
                    self.on_message(msg)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("스트림 끊김: %s: %s", type(e).__name__, e)
            self.connected.clear()
            self.stats.stream_drops += 1
            if _time.monotonic() - started > 60:
                delay = 1.0
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    def on_message(self, msg: dict) -> None:
        ch, data = msg["ch"], msg["data"]
        self.stats.messages[ch.split(":")[0]] += 1
        if ch == "orders":
            self.on_order(data)
        elif ch.startswith("tick:"):
            self.stats.ticks[data["symbol"]] += 1
        elif ch.startswith("candle.1m:") and data.get("closed"):
            self.on_bar(data)
        elif ch == "system":
            t = data.get("type")
            if t == "ERROR":
                self.error(f"서버 오류 메시지 {data.get('code')}: {data.get('message')}")
            elif t in ("HALT", "RESUME", "TRADING_ENABLED"):
                log.warning("시스템 이벤트 %s", data)

    def on_order(self, o: dict) -> None:
        """주문 상태 반영. REST 응답과 WS 이벤트가 순서 없이 오므로, 체결 수량은 늘어난 만큼만 반영하고
        끝난 주문에 늦게 온 이전 상태는 무시한다."""
        oid = o["id"]
        if oid in self.done:
            return
        prev = self.filled.get(oid, 0)
        if o["filled_qty"] > prev:
            delta = o["filled_qty"] - prev
            self.positions[o["symbol"]] = self.positions.get(o["symbol"], 0) + (delta if o["side"] == "buy" else -delta)
            self.filled[oid] = o["filled_qty"]
        if o["status"] == "REJECTED":
            self.stats.rejects.append(f"{o['symbol']} {o['side']} {o.get('reject_reason')}")
        if o["status"] in TERMINAL:
            self.done.add(oid)
            self.orders.pop(oid, None)
            self.order_seen_at.pop(oid, None)
        else:
            self.orders[oid] = o
            self.order_seen_at.setdefault(oid, self.clock())

    def on_bar(self, bar: dict) -> None:
        sym = bar["symbol"]
        if sym not in self.signals:
            return
        ts = datetime.fromisoformat(bar["ts"])
        self.bars[sym].add(bar, ts)
        st = self.signals[sym]
        close = Decimal(bar["close"])
        sma = st.push(close)
        now = self.clock()
        if not self.in_trade_window(now):
            return
        if any(o["symbol"] == sym for o in self.orders.values()):
            return   # 진행 중 주문이 있으면 다음 봉에서
        action = st.decide(close, sma, self.positions.get(sym, 0), now)
        if action == "buy":
            st.entries_today += 1
            st.entered_at = now
            self.spawn(self.place(sym, "buy", 1, kind="entry"))
        elif action == "sell":
            st.entered_at = None
            self.spawn(self.place(sym, "sell", self.positions[sym], kind="exit"))

    def in_trade_window(self, now: datetime) -> bool:
        local = now.astimezone(KST)
        return bool(self.trading_day) and TRADE_START <= local.time() < TRADE_END and self.connected.is_set()

    # ───────────── 주문 ─────────────
    async def place(self, sym: str, side: str, qty: int, price: str | None = None, *, kind: str) -> dict | None:
        self.stats.orders[kind] += 1
        cid = f"pilot-{kind}-{uuid.uuid4().hex[:12]}"
        t0 = _time.perf_counter()
        try:
            o = await self.cs.place(sym, side, qty, price, client_order_id=cid)
        except CentralError as e:
            self.stats.rejects.append(f"{sym} {side} {kind} {e.code}")
            log.warning("주문 거부 %s %s %s: %s %s", sym, side, kind, e.code, e.message)
            return None
        self.stats.latency_ms.append((_time.perf_counter() - t0) * 1000)
        self.on_order(o)
        log.info("주문 %s %s %s %d주 → #%d %s", kind, sym, side, qty, o["id"], o["status"])
        return o

    async def wait_status(self, oid: int, wanted: set[str], timeout: float = 20) -> str:
        deadline = _time.monotonic() + timeout
        status = ""
        while _time.monotonic() < deadline:
            status = (await self.cs.order(oid))["status"]
            if status in wanted:
                return status
            await asyncio.sleep(1)
        return status

    async def probe(self) -> None:
        """지정가 접수 → 정정 → 취소 (체결되지 않을 가격). 주문 경로의 접수/정정/취소를 매번 확인한다."""
        sym = SYMBOLS[0]
        q = await self.cs.quote(sym)
        o = await self.place(sym, "buy", 1, q["lower_limit"], kind="probe")
        if o is None:
            self.stats.probes_failed.append("접수 거부")
            return
        steps = []
        try:
            st = await self.wait_status(o["id"], {"ACCEPTED"} | TERMINAL)
            steps.append(f"접수 {st}")
            if st != "ACCEPTED":
                raise RuntimeError("접수 확인 실패")
            book = await self.cs.orderbook(sym)
            far = book["bids"][-1][0] if book.get("bids") else q["lower_limit"]
            new = await self.cs.amend(o["id"], far)
            self.on_order(new)
            st = await self.wait_status(new["id"], {"ACCEPTED"} | TERMINAL)
            steps.append(f"정정 {st}")
            if st != "ACCEPTED":
                raise RuntimeError("정정 확인 실패")
            await self.cs.cancel(new["id"])
            st = await self.wait_status(new["id"], TERMINAL)
            steps.append(f"취소 {st}")
            if st != "CANCELED":
                raise RuntimeError("취소 확인 실패")
            self.stats.probes_ok += 1
        except (CentralError, RuntimeError) as e:
            msg = f"시험 주문 실패 ({' > '.join(steps)}): {e}"
            self.stats.probes_failed.append(msg)
            self.error(msg)
            try:
                await self.cs.cancel_all("KR")
            except CentralError:
                pass

    async def flatten(self) -> None:
        """장 마감 전 정리: 미체결 취소 후 보유 전량 시장가 매도."""
        try:
            await self.cs.cancel_all("KR")
        except CentralError as e:
            self.error(f"일괄 취소 실패 {e.code}")
        await asyncio.sleep(2)
        for p in await self.cs.positions("KR"):
            if p["symbol"] in SYMBOLS and p["sellable"] > 0:
                await self.place(p["symbol"], "sell", p["sellable"], kind="flatten")
        for st in self.signals.values():
            st.entered_at = None

    # ───────────── 점검 ─────────────
    async def check(self) -> None:
        rest = {p["symbol"]: p["qty"] for p in await self.cs.positions("KR")}
        mine = {k: v for k, v in self.positions.items() if v}
        if rest != mine:
            self.stats.check_failures.append(f"{self.clock():%H:%M} 보유 불일치 이벤트 {mine} / REST {rest}")
            self.error(f"보유 불일치: 이벤트 누적 {mine} / REST {rest} (REST 기준으로 맞춤)")
            self.positions = dict(rest)
        now = self.clock()
        for oid, seen in list(self.order_seen_at.items()):
            o = self.orders.get(oid)
            if o is None or now - seen < STUCK_AFTER:
                continue
            fresh = await self.cs.order(oid)
            if fresh["status"] in TERMINAL:
                self.on_order(fresh)   # 이벤트를 놓쳤다
                self.stats.check_failures.append(f"{now:%H:%M} 주문 #{oid} 종료 이벤트 누락 ({fresh['status']})")
                log.warning("주문 #%d 종료 이벤트 누락: %s", oid, fresh["status"])
            elif fresh["order_type"] == "market" or fresh["status"] in ("SUBMITTING", "UNKNOWN"):
                self.stats.check_failures.append(f"{now:%H:%M} 주문 #{oid} {fresh['status']} 지속")
                self.error(f"주문 #{oid} {fresh['order_type']} {fresh['status']} 상태로 {STUCK_AFTER} 넘게 머묾")
                self.order_seen_at[oid] = now
        bal = await self.cs.balance()
        krw = bal.get("KRW", {})
        if krw and Decimal(krw.get("available", "0")) < 0:
            self.error(f"가용 현금 음수 {krw}")

    # ───────────── 시계 ─────────────
    async def clock_loop(self) -> None:
        while True:
            try:
                await self.on_clock(self.clock())
            except asyncio.CancelledError:
                raise
            except CentralError as e:
                log.warning("주기 작업 API 오류 %s %s", e.code, e.message)
            except Exception as e:
                self.error(f"주기 작업 오류 {type(e).__name__}: {e}")
            await asyncio.sleep(5)

    async def on_clock(self, now: datetime) -> None:
        local = now.astimezone(KST)
        if local.date() != self.stats.day:
            self.save_state()
            self.stats = DayStats(local.date())
            self.bars = {s: BarCheck() for s in SYMBOLS}
            self.done.clear()
            self.filled = {k: v for k, v in self.filled.items() if k in self.orders}
            for st in self.signals.values():
                st.entries_today = 0
            await self.refresh_day()
        if not self.trading_day:
            return
        if not self.stats.reported and self.report_path(self.stats.day).exists():
            self.stats.reported = True   # 재시작 전에 이미 보고함
        t = local.time()
        if self.in_trade_window(now) and (self.last_probe is None or now - self.last_probe >= PROBE_EVERY):
            self.last_probe = now
            self.spawn(self.probe())
        if TRADE_START <= t < time(15, 30) and (self.last_check is None or now - self.last_check >= CHECK_EVERY):
            self.last_check = now
            await self.check()
        if FLATTEN_AT <= t < time(15, 20) and not self.stats.flattened:
            self.stats.flattened = True
            await self.flatten()
        if t >= REPORT_AT and not self.stats.reported:
            self.stats.reported = True
            await self.report()

    # ───────────── 보고 ─────────────
    async def report(self) -> dict:
        day = self.stats.day
        start = datetime.combine(day, time(9, 1), KST)
        end = datetime.combine(day, time(15, 20), KST)
        if self.stats.stream_connects:   # 오늘 중간에 시작했으면 그 뒤부터만 비교
            first = min((min(b.received) for b in self.bars.values() if b.received), default=start)
            start = max(start, first)
        data_check = {}
        for sym in SYMBOLS:
            try:
                rest = (await self.cs.candles(sym, "1m", start=start, end=end, limit=500))["candles"]
                data_check[sym] = self.bars[sym].compare(rest, start, end)
            except CentralError as e:
                data_check[sym] = {"error": f"{e.code} {e.message}"}
        try:
            pnl = await self.cs.pnl(start=day, end=day)
        except CentralError as e:
            pnl = {"error": e.code}
        fills = await self.cs.fills(start=day, end=day, limit=500)
        lat = sorted(self.stats.latency_ms)
        report = {
            "day": str(day),
            "stream": {"connects": self.stats.stream_connects, "drops": self.stats.stream_drops,
                       "messages": dict(self.stats.messages), "ticks": dict(self.stats.ticks)},
            "orders": dict(self.stats.orders), "fills": len(fills), "rejects": self.stats.rejects[-20:],
            "probes": {"ok": self.stats.probes_ok, "failed": self.stats.probes_failed[-10:]},
            "latency_ms": {"n": len(lat), "p50": _pct(lat, 50), "p95": _pct(lat, 95), "max": lat[-1] if lat else None},
            "pnl": pnl, "data_check": data_check,
            "check_failures": self.stats.check_failures[-20:], "errors": self.stats.errors[-20:],
            "positions_after": {k: v for k, v in self.positions.items() if v},
        }
        path = self.report_path(day)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        self.notifier.notify("warning" if self._bad(report) else "info", f"파일럿 일일 보고 {day}",
                             self.summary(report), key=f"report:{day}")
        log.info("일일 보고 %s", path)
        return report

    def report_path(self, day: date) -> Path:
        return self.state_dir / "reports" / f"{day:%Y%m%d}.json"

    @staticmethod
    def _bad(r: dict) -> bool:
        dc = r["data_check"].values()
        return bool(r["probes"]["failed"] or r["check_failures"] or r["errors"]
                    or any(d.get("error") or d.get("missing_count") or d.get("close_mismatch_count") for d in dc))

    @staticmethod
    def summary(r: dict) -> str:
        s = r["stream"]
        lines = [f"스트림 연결 {s['connects']}회, 끊김 {s['drops']}회, 틱 {s['ticks']}",
                 f"주문 {r['orders']} 체결 {r['fills']}건, 거부 {len(r['rejects'])}건",
                 f"시험 주문(접수/정정/취소) 성공 {r['probes']['ok']}, 실패 {len(r['probes']['failed'])}",
                 f"주문 응답 p50 {r['latency_ms']['p50']}ms p95 {r['latency_ms']['p95']}ms"]
        for sym, d in r["data_check"].items():
            if "error" in d:
                lines.append(f"{sym} 1분봉 비교 실패: {d['error']}")
            else:
                lines.append(f"{sym} 1분봉 실시간/REST {d['realtime_bars']}/{d['rest_bars']}, 누락 {d['missing_count']}, "
                             f"종가 불일치 {d['close_mismatch_count']}, 거래량 차이 {d['volume_diff_pct']}%")
        if r["check_failures"]:
            lines.append("점검 실패: " + "; ".join(r["check_failures"][:5]))
        if r["errors"]:
            lines.append("오류: " + "; ".join(r["errors"][:5]))
        lines.append(f"손익: {json.dumps(r['pnl'], ensure_ascii=False, default=str)[:300]}")
        return "\n".join(lines)

    def save_state(self) -> None:
        """하루 통계를 파일로 남긴다 (재시작해도 그날 기록을 잃지 않게. 보고서와 별개)."""
        path = self.state_dir / "stats" / f"{self.stats.day:%Y%m%d}-{self.clock():%H%M%S}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        s = self.stats
        path.write_text(json.dumps({"day": str(s.day), "connects": s.stream_connects, "drops": s.stream_drops,
                                    "ticks": dict(s.ticks), "orders": dict(s.orders), "errors": s.errors},
                                   ensure_ascii=False, default=str), encoding="utf-8")


def _pct(xs: list[float], p: int) -> float | None:
    if not xs:
        return None
    return round(xs[min(len(xs) - 1, int(len(xs) * p / 100))], 1)
