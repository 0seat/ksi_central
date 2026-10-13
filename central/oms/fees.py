"""수수료/세금 계산. 값은 fee_schedules 테이블에서 읽고, 없으면 0 으로 계산한다 (사용자가 추후 기입)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from central.core.money import ZERO, q_fee
from central.storage.db import Database
from central.storage.models import FeeScheduleRow

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FeeRule:
    fee_rate: Decimal = ZERO
    min_fee: Decimal = ZERO
    tax_rate: Decimal = ZERO


class FeeModel:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._warned: set[tuple[str, str, str]] = set()

    def set_rule(
        self, broker_account_id: str, market: str, side: str, rule: FeeRule,
        venue: str | None = None, effective_from: date = date(2000, 1, 1),
    ) -> None:
        with self.db.session() as s, s.begin():
            s.add(FeeScheduleRow(
                broker_account_id=broker_account_id, market=market, venue=venue, side=side,
                fee_rate=rule.fee_rate, min_fee=rule.min_fee, tax_rate=rule.tax_rate, effective_from=effective_from,
            ))

    def rule(self, broker_account_id: str, market: str, venue: str | None, side: str, on: date) -> FeeRule:
        with self.db.session() as s:
            rows = list(s.scalars(select(FeeScheduleRow).where(
                FeeScheduleRow.broker_account_id == broker_account_id, FeeScheduleRow.market == market,
                FeeScheduleRow.side == side, FeeScheduleRow.effective_from <= on,
            )))
        exact = [r for r in rows if r.venue == venue and venue is not None]
        common = [r for r in rows if r.venue is None]
        pick = exact or common
        if not pick:
            key = (broker_account_id, market, side)
            if key not in self._warned:
                self._warned.add(key)
                log.warning("수수료 설정 없음 %s → 0 으로 계산", key)
            return FeeRule()
        r = max(pick, key=lambda x: x.effective_from)
        return FeeRule(r.fee_rate, r.min_fee, r.tax_rate)

    def max_rate(self, broker_account_id: str, market: str, side: str, on: date) -> Decimal:
        """홀드 계산용: 어느 시장(venue)에서 체결되더라도 부족하지 않도록 가장 높은 요율."""
        with self.db.session() as s:
            venues = {r for r in s.scalars(select(FeeScheduleRow.venue).where(
                FeeScheduleRow.broker_account_id == broker_account_id, FeeScheduleRow.market == market,
            ))}
        rules = [self.rule(broker_account_id, market, v, side, on) for v in (venues | {None})]
        return max(r.fee_rate + r.tax_rate for r in rules)

    def compute(
        self, broker_account_id: str, market: str, venue: str | None, side: str,
        notional: Decimal, currency: str, on: date,
    ) -> tuple[Decimal, Decimal]:
        r = self.rule(broker_account_id, market, venue, side, on)
        fee = q_fee(max(notional * r.fee_rate, r.min_fee), currency) if notional > 0 else ZERO
        tax = q_fee(notional * r.tax_rate, currency)
        return fee, tax
