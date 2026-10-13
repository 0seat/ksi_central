"""대사: 서브계좌 장부 합계와 실계좌 잔고 비교 (docs/PLAN.md 6.8).

- 수량: 종목별로 엄격하게 비교
- 현금: 시장 통화별로 허용오차 안에서 비교. fx_mode=broker 서브계좌가 있으면 통화별 비교가
  의미 없으므로 현금 비교를 건너뛴다 (6.2.3)
불일치하면 해당 실계좌 x 시장의 신규 주문을 정지하고 이벤트를 남긴다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Callable

from sqlalchemy import select

from central.broker.base import Broker
from central.core.events import EventBus
from central.core.instruments import MARKET_CURRENCY
from central.core.market_hours import now_kst
from central.core.money import ZERO
from central.oms import ledger
from central.oms.accounts import record_event, tx
from central.risk.engine import RiskEngine
from central.storage.db import Database
from central.storage.models import PositionRow, ReconciliationRow, SubAccountRow

log = logging.getLogger(__name__)

DEFAULT_CASH_TOLERANCE = {"KRW": Decimal("10"), "USD": Decimal("0.05")}


@dataclass(frozen=True)
class ReconcileResult:
    broker_account_id: str
    market: str
    ok: bool
    diffs: list[str]
    cash_checked: bool


class Reconciler:
    def __init__(
        self, db: Database, bus: EventBus, brokers: dict[str, Broker], risk: RiskEngine,
        *, cash_tolerance: dict[str, Decimal] | None = None, clock: Callable[[], datetime] = now_kst,
    ) -> None:
        self.db, self.bus, self.brokers, self.risk = db, bus, brokers, risk
        self.cash_tolerance = cash_tolerance or DEFAULT_CASH_TOLERANCE
        self.clock = clock

    async def run(self, broker_alias: str, market: str, *, halt_on_mismatch: bool = True) -> ReconcileResult:
        snap = await self.brokers[broker_alias].snapshot(market)
        currency = MARKET_CURRENCY[market]
        diffs: list[str] = []

        with self.db.session() as s:
            subs = list(s.scalars(select(SubAccountRow).where(SubAccountRow.broker_account_id == broker_alias)))
            sub_ids = [x.id for x in subs]
            ours: dict[str, int] = {}
            for p in s.scalars(select(PositionRow).where(PositionRow.sub_account_id.in_(sub_ids))):
                if p.instrument_id.startswith(f"{market}:") and p.qty:
                    ours[p.instrument_id] = ours.get(p.instrument_id, 0) + p.qty
            cash = sum((ledger.cash_balance(s, i, currency) for i in sub_ids), ZERO)
            broker_fx_subs = any(x.fx_mode == "broker" for x in subs)

        theirs = {k: q for k, (q, _) in snap.positions.items() if q}
        for inst in sorted(set(ours) | set(theirs)):
            if ours.get(inst, 0) != theirs.get(inst, 0):
                diffs.append(f"{inst} 수량 장부 {ours.get(inst, 0)} / 실계좌 {theirs.get(inst, 0)}")

        cash_checked = currency in snap.cash and not broker_fx_subs
        if cash_checked:
            gap = cash - snap.cash[currency]
            if abs(gap) > self.cash_tolerance[currency]:
                diffs.append(f"{currency} 현금 장부-실계좌 차이 {gap}")

        ok = not diffs
        now = self.clock()
        with tx(self.db, self.bus) as (s, ev):
            s.add(ReconciliationRow(broker_account_id=broker_alias, market=market, run_at=now, ok=ok, diffs=diffs))
            record_event(s, ev, "RECONCILE", None, {
                "broker_account_id": broker_alias, "market": market, "ok": ok, "diffs": diffs,
                "cash_checked": cash_checked}, now)
        if not ok:
            log.error("[%s/%s] 대사 불일치: %s", broker_alias, market, diffs)
            if halt_on_mismatch:
                self.risk.halt("broker_market", f"{broker_alias}:{market}")
        return ReconcileResult(broker_alias, market, ok, diffs, cash_checked)


async def bootstrap_from_broker(accounts, broker: Broker, broker_alias: str, markets: list[str]) -> dict:
    """첫 실행: 실계좌의 현재 현금과 보유종목을 미배정 서브계좌로 들여온다. 이미 기록이 있으면 거부."""
    from central.oms.errors import OmsError

    unallocated = accounts.unallocated(broker_alias)
    if not accounts.is_empty(unallocated.id):
        raise OmsError("ALREADY_BOOTSTRAPPED", "미배정 서브계좌에 이미 기록이 있음")
    summary: dict = {}
    for market in markets:
        snap = await broker.snapshot(market)
        for cur, amount in snap.cash.items():
            if amount:
                accounts.deposit_unallocated(broker_alias, cur, amount, memo="초기 동기화")
        for inst, (qty, avg) in snap.positions.items():
            if qty:
                accounts.import_position(unallocated.id, inst, MARKET_CURRENCY[market], qty, avg)
        summary[market] = {"cash": {k: str(v) for k, v in snap.cash.items()}, "positions": len(snap.positions)}
    return summary
