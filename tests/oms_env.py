"""OMS 테스트 환경: 메모리 DB + SimBroker + 서브계좌 서비스 조립."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from central.broker.sim import SimBroker
from central.core.events import Event, EventBus
from central.core.instruments import Instrument, InstrumentRegistry
from central.core.market_hours import KST, MarketHours
from central.core.money import CURRENCIES, ZERO
from central.oms.accounts import AccountService
from central.oms.fees import FeeModel
from central.oms.orders import OrderService
from central.risk.engine import RiskEngine
from central.settings import AccountProfile
from central.storage.db import Database

KR_OPEN = datetime(2026, 10, 14, 10, 0, tzinfo=KST)    # 수요일 10:00 KST
US_OPEN = datetime(2026, 10, 14, 23, 0, tzinfo=KST)    # 10:00 EDT
SAMSUNG = Instrument("KR:005930", "KR", "005930", "KRX", "삼성전자", nxt_tradable=True)
HYNIX = Instrument("KR:000660", "KR", "000660", "KRX", "SK하이닉스", nxt_tradable=True)
SMALL = Instrument("KR:900000", "KR", "900000", "KRX", "NXT 미거래 종목", nxt_tradable=False)
AAPL = Instrument("US:AAPL", "US", "AAPL", "NASD", "Apple")
ALIAS = "real"


class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


@dataclass
class OmsEnv:
    db: Database
    accounts: AccountService
    orders: OrderService
    sim: SimBroker
    fees: FeeModel
    risk: RiskEngine
    clock: Clock
    profile: AccountProfile
    events: list[Event] = field(default_factory=list)

    @property
    def unallocated_id(self) -> int:
        return self.accounts.unallocated(ALIAS).id

    def fund(self, currency: str, amount: str) -> None:
        """실계좌 입금: SimBroker 와 미배정 장부에 동시 반영."""
        self.sim.deposit(currency, Decimal(amount))
        self.accounts.deposit_unallocated(ALIAS, currency, Decimal(amount))

    def new_sub(self, name: str, **cash: str) -> int:
        sub = self.accounts.create_sub_account(ALIAS, name)
        for cur, amount in cash.items():
            self.accounts.transfer(self.unallocated_id, sub.id, cur, Decimal(amount))
        return sub.id

    def cash(self, sub_id: int, currency: str) -> Decimal:
        return self.accounts.balances(sub_id)[currency].cash

    def available(self, sub_id: int, currency: str) -> Decimal:
        return self.accounts.balances(sub_id)[currency].available

    def qty(self, sub_id: int, instrument_id: str) -> int:
        return next((p.qty for p in self.accounts.positions(sub_id) if p.instrument_id == instrument_id), 0)

    def reconcile(self, krw_tolerance: Decimal = Decimal("5")) -> None:
        """서브계좌 합계 == SimBroker 실계좌 상태 (수량 엄격, 현금은 KRW 환산 총액 허용오차)."""
        subs = self.accounts.list(ALIAS)
        qty: dict[str, int] = {}
        cash = {c: ZERO for c in CURRENCIES}
        for sub in subs:
            for p in self.accounts.positions(sub.id):
                qty[p.instrument_id] = qty.get(p.instrument_id, 0) + p.qty
            for c, b in self.accounts.balances(sub.id).items():
                cash[c] += b.cash
        broker_qty = {k: v for k, v in self.sim.positions.items() if v}
        assert qty == broker_qty, f"수량 불일치 ledger={qty} broker={broker_qty}"
        fx = self.sim.fx
        ours = cash["KRW"] + cash["USD"] * fx
        theirs = self.sim.cash["KRW"] + self.sim.cash["USD"] * fx
        assert abs(ours - theirs) <= krw_tolerance, f"현금 불일치 ledger={cash} broker={dict(self.sim.cash)}"


def make_env(*, integrated_margin: bool = True, at: datetime = KR_OPEN, trading_enabled: bool = True) -> OmsEnv:
    db = Database.memory()
    db.create_all()
    bus = EventBus()
    clock = Clock(at)
    accounts = AccountService(db, bus, clock)
    accounts.ensure_broker_account(ALIAS, "real")
    fees = FeeModel(db)
    sim = SimBroker(
        ALIAS, integrated_margin=integrated_margin, clock=clock,
        fee_fn=lambda market, venue, side, notional, cur: fees.compute(ALIAS, market, venue, side, notional, cur, clock().date()),
    )
    sim.set_quote(SAMSUNG.id, Decimal("61000"), upper=Decimal("79300"), lower=Decimal("42700"))
    sim.set_quote(AAPL.id, Decimal("187.25"))
    profile = AccountProfile(
        env="real", markets=["KR", "US"], kr_routes=["KRX", "NXT", "SOR"], kr_default_route="SOR",
        integrated_margin=integrated_margin, trading_enabled=trading_enabled,
    )
    risk = RiskEngine(db)
    orders = OrderService(
        db, bus, InstrumentRegistry([SAMSUNG, HYNIX, SMALL, AAPL]), {ALIAS: sim}, {ALIAS: profile},
        fees, risk, MarketHours(), clock=clock,
    )
    env = OmsEnv(db, accounts, orders, sim, fees, risk, clock, profile)
    bus.subscribe(env.events.append)
    return env
