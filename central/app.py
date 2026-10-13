"""중앙증권 조립: 설정 → DB → 브로커 → OMS/동기화/대사.

API 서버(M4)와 점검 도구가 이 객체 하나를 만들어 쓴다.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

import httpx

from central.broker.base import Broker
from central.broker.kis.broker import KisBroker
from central.core.events import EventBus
from central.core.instruments import InstrumentRegistry
from central.core.market_hours import MarketHours, now_kst
from central.oms.accounts import AccountService
from central.oms.fees import FeeModel
from central.oms.orders import OrderService
from central.oms.sync import OrderSync
from central.reconcile.reconciler import Reconciler, bootstrap_from_broker
from central.risk.engine import RiskEngine
from central.settings import Settings
from central.storage.db import Database

log = logging.getLogger(__name__)


class CentralApp:
    def __init__(
        self, settings: Settings, *, db: Database | None = None, brokers: dict[str, Broker] | None = None,
        instruments: InstrumentRegistry | None = None, clock: Callable[[], datetime] = now_kst,
    ) -> None:
        self.settings = settings
        self.clock = clock
        self.db = db or Database.file(settings.data_dir / "central.db")
        self.db.create_all()
        self.bus = EventBus()
        self.instruments = instruments or InstrumentRegistry()
        self.http = httpx.AsyncClient(timeout=10.0)
        self.brokers: dict[str, Broker] = brokers or {
            alias: KisBroker(acc, self.http, settings.tokens_dir, clock=clock) for alias, acc in settings.accounts.items()
        }
        self.profiles = {alias: acc.profile for alias, acc in settings.accounts.items()}

        self.accounts = AccountService(self.db, self.bus, clock)
        for alias, acc in settings.accounts.items():
            self.accounts.ensure_broker_account(alias, acc.profile.env)
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

    def markets_of(self, alias: str) -> list[str]:
        return list(self.profiles[alias].markets)

    async def start(self) -> None:
        for alias, broker in self.brokers.items():
            await broker.start()
            await self.sync_all(alias)

    async def stop(self) -> None:
        for broker in self.brokers.values():
            await broker.stop()
        await self.http.aclose()

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
