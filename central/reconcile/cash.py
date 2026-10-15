"""대사 현금 차이 처리 (docs/PLAN.md 6.8.1).

KIS 에는 주식계좌 입출금 내역 API 가 없어서, 앱이 알려 주지 않은 입출금, 배당, 수수료 모델 오차는
장 마감 대사에서 '실계좌 현금 - 장부 현금' 차이로만 드러난다. 수량이 모두 맞고 접수 불확실 주문이 없을 때에만
아래 순서로 설명하고, 설명한 만큼 장부에 기록한다. 하나라도 조건이 안 맞으면 기록하지 않고 불일치로 남긴다.

  1. 배당 (broker.cash_rights): 그날 지급된 권리 금액의 합이 차이와 맞으면 기준일 보유 서브계좌에 수량 비례 DIVIDEND.
     기준일 보유 수량을 장부에서 다시 계산해 실계좌 수량과 맞을 때만. 안 맞으면 미배정에 넣고 알린다.
  2. 수수료 오차: 오늘 체결이 있고 남은 차이가 체결대금의 fee_gap_bp 이내면 체결대금 비례로 FEE (보정).
  3. 미확인 입출금: 남은 차이를 미배정 서브계좌의 UNIDENTIFIED 로. 출금(음수)은 미배정 주문 가능 현금 안에서만.
     서브계좌에 배정된 돈이 빠져나간 것으로 보이면 누구 돈인지 정할 수 없으므로 불일치(정지).

기록한 항목은 이벤트(CASH_RESOLVED)와 알림으로 남고, 관리자가 재분류할 수 있다
(UNIDENTIFIED → 배당: POST /admin/dividends source=unidentified).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from central.broker.base import CashRight
from central.core.market_hours import KST, NEW_YORK
from central.core.money import ZERO, q_cash
from central.oms import ledger
from central.oms.accounts import UNALLOCATED, record_event
from central.oms.errors import OmsError
from central.storage.models import FillRow, LedgerRow, OrderRow, PositionRow, SubAccountRow

UNSETTLED_ORDER = ("SUBMITTING", "UNKNOWN")


def split(amount: Decimal, weights: dict[int, Decimal], currency: str) -> dict[int, Decimal]:
    """금액을 가중치 비례로 나눈다. 반올림 차이는 가장 큰 몫에 얹어 합이 정확히 amount 가 되게."""
    total = sum(weights.values(), ZERO)
    if total <= 0:
        raise ValueError("가중치 합이 0")
    parts = {k: q_cash(amount * w / total, currency) for k, w in weights.items() if w > 0}
    biggest = max(parts, key=lambda k: weights[k])
    parts[biggest] += amount - sum(parts.values(), ZERO)
    return {k: v for k, v in parts.items() if v != 0}


def holdings_at(s: Session, broker_alias: str, instrument_id: str, day: date | None) -> dict[int, int]:
    """서브계좌별 보유 수량. day 를 주면 그날 장 마감 기준으로 되돌린다 (그 뒤 체결을 뺌).
    체결 외 이동(보유종목 이동, 초기 적재)은 되돌리지 못하므로 호출 측이 실계좌 수량과 맞는지 확인한다."""
    subs = [x.id for x in s.scalars(select(SubAccountRow).where(SubAccountRow.broker_account_id == broker_alias))]
    qty: dict[int, int] = defaultdict(int)
    for p in s.scalars(select(PositionRow).where(PositionRow.sub_account_id.in_(subs),
                                                 PositionRow.instrument_id == instrument_id)):
        qty[p.sub_account_id] += p.qty
    if day is not None:
        zone = KST if instrument_id.startswith("KR:") else NEW_YORK
        after = datetime.combine(day, time.max, zone)
        for f in s.scalars(select(FillRow).where(FillRow.broker_account_id == broker_alias,
                                                 FillRow.instrument_id == instrument_id, FillRow.filled_at > after)):
            qty[f.sub_account_id] -= f.qty if f.side == "buy" else -f.qty
    return {k: v for k, v in qty.items() if v > 0}


def post_dividend(s: Session, ev: list, broker_alias: str, instrument_id: str, currency: str, amount: Decimal,
                  holders: dict[int, int], *, at: datetime, memo: str, from_unidentified: bool = False) -> dict[int, Decimal]:
    """세후 배당 amount 를 보유 수량 비례로 DIVIDEND. from_unidentified 면 미배정의 UNIDENTIFIED 에서 옮긴다."""
    if not holders:
        raise OmsError("INVALID_REQUEST", f"{instrument_id} 보유 서브계좌가 없음 (allocations 로 지정)")
    un = s.scalar(select(SubAccountRow).where(SubAccountRow.broker_account_id == broker_alias,
                                              SubAccountRow.name == UNALLOCATED))
    if from_unidentified:
        left = s.scalar(select(func.coalesce(func.sum(LedgerRow.amount), 0)).where(
            LedgerRow.sub_account_id == un.id, LedgerRow.currency == currency, LedgerRow.type == "UNIDENTIFIED"))
        if Decimal(str(left)) < amount:
            raise OmsError("INSUFFICIENT_CASH", f"미배정의 미확인 입금 {currency} {left} < {amount}")
        if ledger.available_cash(s, un.id, currency) < amount:
            raise OmsError("INSUFFICIENT_CASH", f"미배정 주문 가능 {currency} 부족")
        ledger.post(s, un.id, currency, "UNIDENTIFIED", -amount, at=at, memo=f"배당으로 재분류 {instrument_id}")
    parts = split(amount, {k: Decimal(v) for k, v in holders.items()}, currency)
    for sub_id, part in parts.items():
        ledger.post(s, sub_id, currency, "DIVIDEND", part, at=at, ref_type="instrument", memo=f"{instrument_id} {memo}")
        record_event(s, ev, "DIVIDEND", sub_id, {"instrument_id": instrument_id, "currency": currency,
                                                 "amount": str(part), "memo": memo}, at)
    return parts


@dataclass
class CashResolution:
    remaining: Decimal                 # 설명하지 못한 차이 (실계좌 - 장부)
    lines: list[str]                   # 기록한 것 (알림용)
    blocked: str | None = None         # 자동 처리하지 않은 이유


def resolve(s: Session, ev: list, broker_alias: str, market: str, currency: str, gap: Decimal,
            rights: list[CashRight], *, tolerance: Decimal, fee_gap_bp: Decimal, day: date,
            at: datetime) -> CashResolution:
    """gap = 실계좌 현금 - 장부 현금. 세션 안에서 장부에 기록하고 남은 차이를 돌려준다."""
    lines: list[str] = []
    un = s.scalar(select(SubAccountRow).where(SubAccountRow.broker_account_id == broker_alias,
                                              SubAccountRow.name == UNALLOCATED))
    pending = s.scalar(select(func.count()).select_from(OrderRow).where(
        OrderRow.broker_account_id == broker_alias, OrderRow.market == market,
        OrderRow.status.in_(UNSETTLED_ORDER)))
    if pending:
        return CashResolution(gap, lines, f"접수 불확실 주문 {pending}건이 있어 자동 처리 안 함")

    # 1. 배당: 증권사 기록이므로 믿되, 지급 합이 차이보다 크면 필드 해석이 틀린 것으로 보고 쓰지 않는다
    rights = [r for r in rights if r.gross - r.tax > 0]
    total = sum((r.gross - r.tax for r in rights), ZERO)
    if rights and total > gap + tolerance:
        lines.append(f"권리 지급 {currency} {total} 가 현금 차이 {gap} 보다 커서 배당으로 처리하지 않음 (확인 필요)")
        rights = []
    for r in rights:
        net = r.gross - r.tax
        holders = holdings_at(s, broker_alias, r.instrument_id, r.record_date)
        memo = f"배당 기준일 {r.record_date} 세전 {r.gross} 원천징수 {r.tax}"
        if r.qty and sum(holders.values()) == r.qty:
            parts = post_dividend(s, ev, broker_alias, r.instrument_id, currency, net, holders, at=at, memo=memo)
            lines.append(f"배당 {r.instrument_id} {currency} {net} → " +
                         ", ".join(f"#{k} {v}" for k, v in parts.items()))
        else:
            ledger.post(s, un.id, currency, "UNIDENTIFIED", net, at=at, memo=f"{r.instrument_id} {memo} (배정 불가)")
            lines.append(f"배당 {r.instrument_id} {currency} {net} 미배정으로 (기준일 보유 수량을 장부에서 맞출 수 없음, "
                         f"실계좌 {r.qty} / 장부 {sum(holders.values())}). /admin/dividends 로 배정")
        gap -= net
    if abs(gap) <= tolerance:
        return CashResolution(gap, lines)

    # 2. 수수료 모델 오차
    start = datetime.combine(day, time.min, KST if market == "KR" else NEW_YORK)
    traded: dict[int, Decimal] = defaultdict(lambda: ZERO)
    for f in s.scalars(select(FillRow).where(FillRow.broker_account_id == broker_alias, FillRow.currency == currency,
                                             FillRow.instrument_id.like(f"{market}:%"), FillRow.filled_at >= start)):
        traded[f.sub_account_id] += f.price * f.qty
    value = sum(traded.values(), ZERO)
    if value > 0 and abs(gap) <= value * fee_gap_bp / Decimal(10000):
        parts = split(gap, traded, currency)
        for sub_id, part in parts.items():
            ledger.post(s, sub_id, currency, "FEE", part, at=at,
                        memo=f"대사 수수료 보정 (체결대금 {q_cash(value, currency)} 대비 {gap / value * 10000:.2f}bp)")
        lines.append(f"수수료 보정 {currency} {gap} (오늘 체결대금 {q_cash(value, currency)}의 "
                     f"{gap / value * 10000:.2f}bp) → " + ", ".join(f"#{k} {v}" for k, v in parts.items()) +
                     ". 반복되면 수수료표 확인")
        return CashResolution(ZERO, lines)

    # 3. 미확인 입출금 (미배정)
    if gap > 0:
        ledger.post(s, un.id, currency, "UNIDENTIFIED", gap, at=at, memo="대사: 미확인 입금 (입금/배당/환급 추정)")
        lines.append(f"미확인 입금 {currency} {gap} → 미배정. 입금이면 그대로 두거나 이체, 배당이면 /admin/dividends")
        return CashResolution(ZERO, lines)
    if ledger.available_cash(s, un.id, currency) >= -gap:
        ledger.post(s, un.id, currency, "UNIDENTIFIED", gap, at=at, memo="대사: 미확인 출금 (출금/비용 추정)")
        lines.append(f"미확인 출금 {currency} {-gap} ← 미배정")
        return CashResolution(ZERO, lines)
    return CashResolution(gap, lines, f"미확인 출금 {currency} {-gap} 이 미배정 주문 가능 현금보다 큼 "
                                      "(서브계좌에 배정된 돈이 빠져나감)")
