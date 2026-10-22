"""중앙증권 테이블 (docs/PLAN.md 6.3).

원장(cash_ledger)은 append-only 이며 서브계좌 예수금은 원장 합계로 계산한다.
positions 는 체결로부터 갱신되는 파생 상태다.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from central.core.market_hours import now_kst
from central.storage.db import Base, DecimalText


class BrokerAccountRow(Base):
    __tablename__ = "broker_accounts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # 설정의 alias
    env: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_kst)


class SubAccountRow(Base):
    __tablename__ = "sub_accounts"
    __table_args__ = (UniqueConstraint("broker_account_id", "name"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    broker_account_id: Mapped[str] = mapped_column(ForeignKey("broker_accounts.id"))
    name: Mapped[str] = mapped_column(String(64))
    type: Mapped[str] = mapped_column(String(16))  # system / strategy / app
    status: Mapped[str] = mapped_column(String(16), default="active")  # active / suspended / closed
    markets: Mapped[list[str]] = mapped_column(JSON, default=lambda: ["KR", "US"])  # 거래 가능한 시장
    fx_mode: Mapped[str] = mapped_column(String(8), default="none")  # none: 통화별 독립 잔고 / broker: 통합증거금 자동환전
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_kst)


class RiskLimitRow(Base):
    __tablename__ = "risk_limits"
    __table_args__ = (UniqueConstraint("sub_account_id", "market"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sub_account_id: Mapped[int] = mapped_column(ForeignKey("sub_accounts.id"))
    market: Mapped[str] = mapped_column(String(4))
    max_order_amount: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    max_position_amount: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    max_daily_loss: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    max_open_orders: Mapped[int | None] = mapped_column(Integer, nullable=True)
    allowed_symbols: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)


class LedgerRow(Base):
    __tablename__ = "cash_ledger"
    __table_args__ = (Index("ix_ledger_sub_cur", "sub_account_id", "currency"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sub_account_id: Mapped[int] = mapped_column(ForeignKey("sub_accounts.id"))
    currency: Mapped[str] = mapped_column(String(3))
    type: Mapped[str] = mapped_column(String(16))
    amount: Mapped[Decimal] = mapped_column(DecimalText)
    fx_rate: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    provisional: Mapped[bool] = mapped_column(Boolean, default=False)
    ref_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    ref_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    memo: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_kst)


class HoldRow(Base):
    __tablename__ = "holds"
    __table_args__ = (Index("ix_holds_sub_status", "sub_account_id", "status"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sub_account_id: Mapped[int] = mapped_column(ForeignKey("sub_accounts.id"))
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    kind: Mapped[str] = mapped_column(String(8))  # cash / qty
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    instrument_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    original: Mapped[Decimal] = mapped_column(DecimalText)
    remaining: Mapped[Decimal] = mapped_column(DecimalText)
    fx_rate: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)  # 통합증거금 KRW 홀드에 적용한 환율
    status: Mapped[str] = mapped_column(String(16), default="active")  # active / released


class PositionRow(Base):
    __tablename__ = "positions"
    sub_account_id: Mapped[int] = mapped_column(ForeignKey("sub_accounts.id"), primary_key=True)
    instrument_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    currency: Mapped[str] = mapped_column(String(3))
    qty: Mapped[int] = mapped_column(Integer, default=0)
    avg_price: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal("0"))
    realized_pnl: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal("0"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_kst)


class OrderRow(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("sub_account_id", "client_order_id"),
        Index("ix_orders_broker_no", "broker_account_id", "broker_order_no"),
        Index("ix_orders_sub_status", "sub_account_id", "status"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sub_account_id: Mapped[int] = mapped_column(ForeignKey("sub_accounts.id"))
    broker_account_id: Mapped[str] = mapped_column(ForeignKey("broker_accounts.id"))
    client_order_id: Mapped[str] = mapped_column(String(64))
    broker_order_no: Mapped[str | None] = mapped_column(String(32), nullable=True)
    broker_org_no: Mapped[str | None] = mapped_column(String(16), nullable=True)
    orig_order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), nullable=True)
    instrument_id: Mapped[str] = mapped_column(String(32))
    market: Mapped[str] = mapped_column(String(4))
    currency: Mapped[str] = mapped_column(String(3))
    side: Mapped[str] = mapped_column(String(4))
    order_type: Mapped[str] = mapped_column(String(8))
    route: Mapped[str | None] = mapped_column(String(8), nullable=True)
    price: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    qty: Mapped[int] = mapped_column(Integer)
    filled_qty: Mapped[int] = mapped_column(Integer, default=0)
    avg_fill_price: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    status: Mapped[str] = mapped_column(String(20))
    reject_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_kst)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_kst)


class FillRow(Base):
    __tablename__ = "fills"
    __table_args__ = (UniqueConstraint("broker_account_id", "broker_fill_key"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    broker_account_id: Mapped[str] = mapped_column(ForeignKey("broker_accounts.id"))
    broker_fill_key: Mapped[str] = mapped_column(String(64))
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), nullable=True)
    sub_account_id: Mapped[int] = mapped_column(ForeignKey("sub_accounts.id"))
    instrument_id: Mapped[str] = mapped_column(String(32))
    venue: Mapped[str | None] = mapped_column(String(8), nullable=True)
    side: Mapped[str] = mapped_column(String(4))
    price: Mapped[Decimal] = mapped_column(DecimalText)
    qty: Mapped[int] = mapped_column(Integer)
    fee: Mapped[Decimal] = mapped_column(DecimalText)
    tax: Mapped[Decimal] = mapped_column(DecimalText)
    realized_pnl: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String(3))
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FeeScheduleRow(Base):
    __tablename__ = "fee_schedules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    broker_account_id: Mapped[str] = mapped_column(ForeignKey("broker_accounts.id"))
    market: Mapped[str] = mapped_column(String(4))
    venue: Mapped[str | None] = mapped_column(String(8), nullable=True)  # None: 시장 공통
    side: Mapped[str] = mapped_column(String(4))
    fee_rate: Mapped[Decimal] = mapped_column(DecimalText)
    min_fee: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal("0"))
    tax_rate: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal("0"))
    effective_from: Mapped[date] = mapped_column(Date)


class ReconciliationRow(Base):
    __tablename__ = "reconciliations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    broker_account_id: Mapped[str] = mapped_column(ForeignKey("broker_accounts.id"))
    market: Mapped[str] = mapped_column(String(4))
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_kst)
    ok: Mapped[bool] = mapped_column(Boolean)
    diffs: Mapped[list[str]] = mapped_column(JSON)


class ApiKeyRow(Base):
    """서브계좌 API 키. 평문은 발급 응답에서 한 번만 보여 주고 DB 에는 SHA-256 해시만 둔다."""
    __tablename__ = "api_keys"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sub_account_id: Mapped[int] = mapped_column(ForeignKey("sub_accounts.id"))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    prefix: Mapped[str] = mapped_column(String(16))  # 식별용 앞부분 (예: cs_AbCd12)
    label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_kst)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class InstrumentRow(Base):
    """종목 정보. M5 종목 마스터 전까지는 관리자가 등록한다 (미국 종목은 거래소 코드가 주문에 필요)."""
    __tablename__ = "instruments"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # KR:005930
    market: Mapped[str] = mapped_column(String(4))
    code: Mapped[str] = mapped_column(String(16))
    exchange: Mapped[str] = mapped_column(String(8))
    name: Mapped[str] = mapped_column(String(80), default="")
    nxt_tradable: Mapped[bool] = mapped_column(Boolean, default=False)
    kind: Mapped[str] = mapped_column(String(8), default="stock")      # stock / etf / etn / other
    name_en: Mapped[str] = mapped_column(String(80), default="")
    source: Mapped[str] = mapped_column(String(8), default="manual")   # manual / master
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_kst)


class MarketDayRow(Base):
    """거래소 개장일 (국내: KIS 휴장일 조회 CTCA0903R 결과). 없는 날짜는 평일=개장으로 추정한다."""
    __tablename__ = "market_days"
    market: Mapped[str] = mapped_column(String(4), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[bool] = mapped_column(Boolean)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_kst)


class CandleRow(Base):
    """완성된 봉 캐시 (진행 중인 봉은 저장하지 않는다). interval: 1m / 1d / 1w / 1M"""
    __tablename__ = "candles"
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    interval: Mapped[str] = mapped_column(String(4), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    open: Mapped[Decimal] = mapped_column(DecimalText)
    high: Mapped[Decimal] = mapped_column(DecimalText)
    low: Mapped[Decimal] = mapped_column(DecimalText)
    close: Mapped[Decimal] = mapped_column(DecimalText)
    volume: Mapped[int] = mapped_column(Integer)


class CandleCoverageRow(Base):
    """KIS 에서 빠짐없이 받아 캐시한 구간. 이 안의 요청은 KIS 를 다시 부르지 않는다."""
    __tablename__ = "candle_coverage"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32))
    interval: Mapped[str] = mapped_column(String(4))
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_kst)


class HaltRow(Base):
    """긴급 정지 상태. 재시작해도 유지된다 (대사 불일치로 걸린 정지가 재시작으로 풀리지 않게)."""
    __tablename__ = "halts"
    scope: Mapped[str] = mapped_column(String(16), primary_key=True)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_kst)


class JobRunRow(Base):
    """운영 스케줄러 작업의 마지막 실행 (작업 기준 시간대의 날짜). 재시작 후 같은 날 중복 실행 방지."""
    __tablename__ = "job_runs"
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_date: Mapped[date] = mapped_column(Date)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_kst)
    ok: Mapped[bool] = mapped_column(Boolean)
    detail: Mapped[str] = mapped_column(String(2000), default="")


class EventRow(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_kst)
    type: Mapped[str] = mapped_column(String(32))
    sub_account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class HistorySubscriptionRow(Base):
    """앱이 API 로 등록한 과거 시세 보관 대상 (central/marketdata/history_targets.py). owner: sub:<id> 또는 admin."""
    __tablename__ = "history_subscriptions"
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_kst)


class UiLayoutRow(Base):
    """대시보드 레이아웃 (central/api/ui.py). owner: admin 또는 sub:<id>. layout: {"pages": [...]}"""
    __tablename__ = "ui_layouts"
    owner: Mapped[str] = mapped_column(String(32), primary_key=True)
    layout: Mapped[dict[str, Any]] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_kst)
