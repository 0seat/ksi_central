"""중앙증권 조립: 설정 → DB → 브로커 → OMS/동기화/대사.

API 서버(central/api)와 점검 도구가 이 객체 하나를 만들어 쓴다.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Callable

import httpx
from sqlalchemy import select

from central.broker.base import Broker
from central.broker.kis.broker import KisBroker
from central.core.events import EventBus
from central.core.instruments import InstrumentRegistry
from central.core.market_hours import MarketHours, now_kst
from central.marketdata.calendar import CalendarService
from central.marketdata.candles import CandleService
from central.marketdata.master import fetch_master
from central.marketdata.hub import MarketDataHub, RealtimeSource
from central.marketdata.ticks import TickStore
from central.oms.accounts import AccountService, record_event, tx
from central.oms.apikeys import ApiKeyService
from central.oms.catalog import InstrumentCatalog
from central.oms.errors import OmsError
from central.oms.fees import FeeModel
from central.oms.orders import WORKING, OrderService
from central.oms.sync import OrderSync
from central.ops.notify import Notifier, notifier_from_settings
from central.ops.service import Operations
from central.reconcile.reconciler import Reconciler, bootstrap_from_broker
from central.risk.engine import RiskEngine
from central.settings import Settings
from central.storage.db import Database
from central.storage.models import OrderRow, SubAccountRow

log = logging.getLogger(__name__)

HALT_SCOPES = ("all", "broker", "broker_market", "sub", "market")


class CentralApp:
    def __init__(
        self, settings: Settings, *, db: Database | None = None, brokers: dict[str, Broker] | None = None,
        instruments: InstrumentRegistry | None = None, clock: Callable[[], datetime] = now_kst,
        realtime: dict[str, RealtimeSource] | None = None, notifier: Notifier | None = None,
    ) -> None:
        """realtime: 시세 등록에 쓸 계좌별 실시간 세션. 생략하면 KisBroker 의 공유 세션을 쓴다.
        notifier: 생략하면 secrets.toml 의 Discord 웹훅 (없으면 알림 꺼짐)."""
        self.settings = settings
        self.notifier = notifier or notifier_from_settings(settings)
        self.clock = clock
        self.db = db or Database.file(settings.data_dir / "central.db")
        self.db.migrate()
        self.bus = EventBus()
        self.instruments = instruments or InstrumentRegistry()
        self.catalog = InstrumentCatalog(self.db, self.instruments, clock)
        self.catalog.load()
        self.http = httpx.AsyncClient(timeout=10.0)
        self.brokers: dict[str, Broker] = brokers or {
            alias: KisBroker(acc, self.http, settings.tokens_dir, clock=clock) for alias, acc in settings.accounts.items()
        }
        self.profiles = {alias: acc.profile for alias, acc in settings.accounts.items()}

        self.accounts = AccountService(self.db, self.bus, clock)
        for alias, acc in settings.accounts.items():
            self.accounts.ensure_broker_account(alias, acc.profile.env)
        self.apikeys = ApiKeyService(self.db, self.bus, clock)
        self.fees = FeeModel(self.db)
        self.risk = RiskEngine(self.db)
        self.hours = MarketHours()
        self.orders = OrderService(
            self.db, self.bus, self.instruments, self.brokers, self.profiles, self.fees, self.risk, self.hours,
            clock=clock,
        )
        self.sync = OrderSync(self.orders, clock=clock)
        self.reconciler = Reconciler(self.db, self.bus, self.brokers, self.risk, clock=clock)

        for alias, broker in self.brokers.items():
            if isinstance(broker, KisBroker):
                broker.on_reconnect = lambda alias=alias: self.sync_all(alias)

        if realtime is None:
            realtime = {a: b.session for a, b in self.brokers.items() if isinstance(b, KisBroker)}
        self.market = MarketDataHub(realtime, self.profiles, self.catalog, settings.market_data, clock)
        self.ticks: TickStore | None = None
        if settings.market_data.store_ticks:
            self.ticks = TickStore(settings.ticks_dir, flush_seconds=settings.market_data.flush_seconds, clock=clock)
            self.market.listeners.append(self.ticks.on_market)
        self.candles = CandleService(self.db, self.data_broker, clock)
        self.price_limits: dict[tuple, tuple] = {}   # (종목, 날짜) → (상한가, 하한가) 현재가 응답용 캐시
        self.calendar = CalendarService(self.db, self.hours, self._kr_calendar_broker, clock)
        self.calendar.apply()
        self.ops = Operations(self, self.notifier)

    def _kr_calendar_broker(self) -> Broker | None:
        """국내 휴장일 조회(CTCA0903R)는 실전 계좌만 된다."""
        return next((self.brokers[a] for a, p in self.profiles.items() if p.env == "real" and "KR" in p.markets), None)

    async def refresh_master(self, markets: tuple[str, ...] = ("KR", "US")) -> dict[str, int]:
        items = await fetch_master(self.http, markets)
        n = self.catalog.bulk_load(items)
        log.info("종목 마스터 적재 %d건 %s", n, self.catalog.counts())
        return self.catalog.counts()

    async def refresh_reference_data(self) -> None:
        """휴장일(하루 한 번)과 종목 마스터(비어 있을 때). 실패해도 서버는 계속 뜬다."""
        try:
            await self.calendar.refresh_kr()
        except Exception as e:
            log.error("국내 휴장일 갱신 실패 (평일=개장으로 추정): %s", e)
        self.calendar.apply()
        if not self.catalog.has_master():
            try:
                await self.refresh_master()
            except Exception as e:
                log.error("종목 마스터 적재 실패 (국내는 자동 등록, 미국은 관리자 등록 필요): %s", e)

    def data_broker(self, market: str) -> Broker:
        """REST 시세/캔들/호가에 쓸 실계좌: 시세 허브와 같은 계좌, 없으면 해당 시장을 지원하는 실전 계좌 우선."""
        try:
            return self.brokers[self.market.source_account(market)]
        except OmsError:
            pass
        candidates = sorted((a for a, p in self.profiles.items() if market in p.markets),
                            key=lambda a: self.profiles[a].env != "real")
        if not candidates:
            raise OmsError("MARKET_NOT_SUPPORTED", f"{market} 을 지원하는 실계좌 없음")
        return self.brokers[candidates[0]]

    def markets_of(self, alias: str) -> list[str]:
        return list(self.profiles[alias].markets)

    async def start(self) -> None:
        await self.refresh_reference_data()
        for alias, broker in self.brokers.items():
            await broker.start()
            await self.sync_all(alias)
        if self.ticks:
            await self.ticks.start()
        await self.market.start_watchlist()
        await self.ops.start()

    async def stop(self) -> None:
        await self.ops.stop()
        if self.ticks:
            await self.ticks.stop()
        for broker in self.brokers.values():
            await broker.stop()
        await self.http.aclose()
        await asyncio.to_thread(self.notifier.close)

    async def sync_all(self, alias: str) -> None:
        for market in self.markets_of(alias):
            try:
                await self.sync.run(alias, market)
            except Exception:
                log.exception("[%s/%s] 주문 동기화 실패", alias, market)

    async def bootstrap_if_empty(self, alias: str) -> dict | None:
        unallocated = self.accounts.unallocated(alias)
        if not self.accounts.is_empty(unallocated.id):
            return None
        return await bootstrap_from_broker(self.accounts, self.brokers[alias], alias, self.markets_of(alias))

    # ───────────── 관리 기능 ─────────────
    def set_trading_enabled(self, alias: str, enabled: bool) -> None:
        """실행 중에만 적용 (재시작하면 central.toml 값으로 돌아간다)."""
        if alias not in self.profiles:
            raise OmsError("NOT_FOUND", f"실계좌 {alias} 없음")
        self.profiles[alias] = self.profiles[alias].model_copy(update={"trading_enabled": enabled})
        self._system_event("TRADING_ENABLED", {"broker_account_id": alias, "enabled": enabled})

    def halt(self, scope: str, id_: str = "") -> None:
        self._check_scope(scope, id_)
        self.risk.halt(scope, id_)
        self._system_event("HALT", {"scope": scope, "id": id_})

    def resume(self, scope: str, id_: str = "") -> None:
        self._check_scope(scope, id_)
        self.risk.resume(scope, id_)
        self._system_event("RESUME", {"scope": scope, "id": id_})

    def halted(self) -> list[dict]:
        return [{"scope": s, "id": i} for s, i in sorted(self.risk.halted)]

    async def cancel_open_orders(self, scope: str, id_: str = "", sub_account_id: int | None = None,
                                 market: str | None = None) -> dict:
        """범위 안의 미체결 주문을 모두 취소 요청. 실패한 건은 사유와 함께 돌려준다."""
        self._check_scope(scope, id_)
        with self.db.session() as s:
            q = select(OrderRow.id, OrderRow.sub_account_id).where(OrderRow.status.in_(WORKING))
            if scope == "broker":
                q = q.where(OrderRow.broker_account_id == id_)
            elif scope == "broker_market":
                alias, _, mkt = id_.partition(":")
                q = q.where(OrderRow.broker_account_id == alias, OrderRow.market == mkt)
            elif scope == "sub":
                q = q.where(OrderRow.sub_account_id == int(id_))
            elif scope == "market":
                q = q.where(OrderRow.market == id_)
            if sub_account_id is not None:
                q = q.where(OrderRow.sub_account_id == sub_account_id)
            if market:
                q = q.where(OrderRow.market == market)
            targets = list(s.execute(q.order_by(OrderRow.id)))
        done, failed = [], []
        for order_id, sub_id in targets:
            try:
                await self.orders.cancel(sub_id, order_id)
                done.append(order_id)
            except Exception as e:
                failed.append({"order_id": order_id, "error": str(e)})
        return {"requested": done, "failed": failed}

    def _check_scope(self, scope: str, id_: str) -> None:
        if scope not in HALT_SCOPES:
            raise OmsError("INVALID_REQUEST", f"scope 는 {HALT_SCOPES} 중 하나")
        if scope == "all":
            return
        if not id_:
            raise OmsError("INVALID_REQUEST", f"scope={scope} 에는 id 가 필요")
        if scope == "broker" and id_ not in self.profiles:
            raise OmsError("NOT_FOUND", f"실계좌 {id_} 없음")
        if scope == "broker_market":
            alias, _, mkt = id_.partition(":")
            if alias not in self.profiles or mkt not in ("KR", "US"):
                raise OmsError("INVALID_REQUEST", "broker_market 의 id 는 '<alias>:<KR|US>'")
        if scope == "market" and id_ not in ("KR", "US"):
            raise OmsError("INVALID_REQUEST", "market 의 id 는 KR 또는 US")
        if scope == "sub":
            with self.db.session() as s:
                if not id_.isdigit() or s.get(SubAccountRow, int(id_)) is None:
                    raise OmsError("NOT_FOUND", f"서브계좌 {id_} 없음")

    def _system_event(self, type_: str, payload: dict) -> None:
        with tx(self.db, self.bus) as (s, ev):
            record_event(s, ev, type_, None, payload, self.clock())
