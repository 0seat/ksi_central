"""실시간 체결통보(H0STCNI0/9, H0GSCNI0/9) 해석.

컬럼 순서는 공식 샘플(examples_user/*_functions_ws.py ccnl_notice) 기준.
값의 의미 (KIS 문서 기준, 실데이터로 재확인 필요한 항목은 VERIFY 표시)
- SELN_BYOV_CLS: 01 매도, 02 매수
- RCTF_CLS: 0 정상, 1 정정, 2 취소
- CNTG_YN: 1 주문/정정/취소/거부 접수 통보, 2 체결 통보
- RFUS_YN: 0 승인, 1 거부
- ACPT_YN: 1 주문접수, 2 확인, 3 취소(IOC/FOK)  VERIFY
- ORD_EXG_GB: 체결/주문 거래소 구분 (1 KRX, 2 NXT 로 추정)  VERIFY
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from central.broker.base import BrokerCancelled, BrokerEvent, BrokerFill, BrokerOrderRejected
from central.core.market_hours import KST

log = logging.getLogger(__name__)

KR_COLUMNS = [
    "CUST_ID", "ACNT_NO", "ODER_NO", "OODER_NO", "SELN_BYOV_CLS", "RCTF_CLS", "ODER_KIND", "ODER_COND",
    "STCK_SHRN_ISCD", "CNTG_QTY", "CNTG_UNPR", "STCK_CNTG_HOUR", "RFUS_YN", "CNTG_YN", "ACPT_YN", "BRNC_NO",
    "ODER_QTY", "ACNT_NAME", "ORD_COND_PRC", "ORD_EXG_GB", "POPUP_YN", "FILLER", "CRDT_CLS", "CRDT_LOAN_DATE",
    "CNTG_ISNM40", "ODER_PRC",
]
US_COLUMNS = [
    "CUST_ID", "ACNT_NO", "ODER_NO", "OODER_NO", "SELN_BYOV_CLS", "RCTF_CLS", "ODER_KIND2", "STCK_SHRN_ISCD",
    "CNTG_QTY", "CNTG_UNPR", "STCK_CNTG_HOUR", "RFUS_YN", "CNTG_YN", "ACPT_YN", "BRNC_NO", "ODER_QTY",
    "ACNT_NAME", "CNTG_ISNM", "ODER_COND", "DEBT_GB", "DEBT_DATE", "START_TM", "END_TM", "TM_DIV_TP",
    "CNTG_UNPR12",
]
# 로그에 남겨도 되는 필드 (계좌번호, 고객ID, 계좌명은 제외)
SAFE_FIELDS = ("ODER_NO", "OODER_NO", "SELN_BYOV_CLS", "RCTF_CLS", "STCK_SHRN_ISCD", "CNTG_QTY", "CNTG_UNPR",
               "STCK_CNTG_HOUR", "RFUS_YN", "CNTG_YN", "ACPT_YN", "ODER_QTY", "ORD_EXG_GB", "ODER_PRC", "CNTG_UNPR12")

KR_VENUES = {"1": "KRX", "2": "NXT"}


class NoticeParser:
    def __init__(self, market: str) -> None:
        self.market = market
        self.columns = KR_COLUMNS if market == "KR" else US_COLUMNS
        self._seen: Counter[str] = Counter()

    def to_dict(self, record: list[str]) -> dict[str, str]:
        if len(record) != len(self.columns):
            log.warning("%s 체결통보 필드 수 %d (예상 %d)", self.market, len(record), len(self.columns))
        return dict(zip(self.columns, (v.strip() for v in record)))

    def safe_view(self, d: dict[str, str]) -> dict[str, str]:
        return {k: d.get(k, "") for k in SAFE_FIELDS if k in d}

    def parse(self, record: list[str], today: date) -> BrokerEvent | None:
        d = self.to_dict(record)
        side = "sell" if d.get("SELN_BYOV_CLS") == "01" else "buy"
        order_no = d.get("ODER_NO", "")
        orig_no = d.get("OODER_NO", "")

        if d.get("RFUS_YN") == "1":
            return BrokerOrderRejected(order_no, "체결통보: 주문 거부")

        if d.get("CNTG_YN") == "2":
            qty = _int(d.get("CNTG_QTY"))
            price = self._price(d)
            if qty <= 0 or price is None:
                log.error("체결통보 해석 실패: %s", self.safe_view(d))
                return None
            hhmmss = d.get("STCK_CNTG_HOUR", "000000")[:6].ljust(6, "0")
            base = f"{today:%Y%m%d}-{order_no}-{hhmmss}-{qty}-{price}"
            self._seen[base] += 1  # 같은 초에 같은 수량/가격 체결이 여러 번 오는 경우 구분
            return BrokerFill(
                broker_order_no=order_no,
                fill_key=f"{base}-{self._seen[base]}",
                instrument_id=f"{self.market}:{d.get('STCK_SHRN_ISCD', '')}",
                side=side,  # type: ignore[arg-type]
                qty=qty,
                price=price,
                venue=KR_VENUES.get(d.get("ORD_EXG_GB", ""), d.get("ORD_EXG_GB") or None) if self.market == "KR" else None,
                filled_at=_time_on(today, hhmmss),
            )

        if d.get("RCTF_CLS") == "2" and orig_no:
            # 취소 주문의 접수/확인 통보 → 원주문의 잔량이 취소됨
            return BrokerCancelled(orig_no, _int(d.get("ODER_QTY")))
        return None

    def _price(self, d: dict[str, str]) -> Decimal | None:
        for key in ("CNTG_UNPR12", "CNTG_UNPR") if self.market == "US" else ("CNTG_UNPR",):
            raw = d.get(key, "")
            if raw:
                try:
                    value = Decimal(raw)
                except InvalidOperation:
                    continue
                if value > 0:
                    return value
        return None


def _int(raw: str | None) -> int:
    try:
        return int(Decimal(raw or "0"))
    except InvalidOperation:
        return 0


def _time_on(day: date, hhmmss: str) -> datetime:
    try:
        return datetime(day.year, day.month, day.day, int(hhmmss[:2]), int(hhmmss[2:4]), int(hhmmss[4:6]), tzinfo=KST)
    except ValueError:
        return datetime.now(KST)
