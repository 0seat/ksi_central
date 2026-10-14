"""종목 정의와 호가단위.

종목 식별자: "{시장}:{코드}" (예: KR:005930, US:AAPL)
종목 마스터 적재(KIS 종목정보 파일)는 M5에서 구현하고, 여기서는 레지스트리만 제공한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from central.core.money import Currency

Market = Literal["KR", "US"]
MARKET_CURRENCY: dict[str, Currency] = {"KR": "KRW", "US": "USD"}

# KRX/NXT 주식 호가단위 (2023-01-25 개편, 유가/코스닥 공통. 2026-10-14 실시간 10단계 호가로 확인).
# ETF/ETN 등은 별도 규칙이라 tick_size 에서 따로 처리한다.
_KR_TICK_BANDS: list[tuple[Decimal, Decimal]] = [
    (Decimal("2000"), Decimal("1")),
    (Decimal("5000"), Decimal("5")),
    (Decimal("20000"), Decimal("10")),
    (Decimal("50000"), Decimal("50")),
    (Decimal("200000"), Decimal("100")),
    (Decimal("500000"), Decimal("500")),
]
_KR_TICK_TOP = Decimal("1000")


@dataclass(frozen=True)
class Instrument:
    id: str
    market: Market
    code: str
    exchange: str  # KRX / NASD / NYSE / AMEX
    name: str = ""
    nxt_tradable: bool = False
    kind: str = "stock"   # stock / etf / etn / other (종목 마스터의 증권 구분)

    @property
    def currency(self) -> Currency:
        return MARKET_CURRENCY[self.market]

    @staticmethod
    def make_id(market: str, code: str) -> str:
        return f"{market}:{code}"


def tick_size(instrument: Instrument, price: Decimal) -> Decimal:
    if instrument.market == "KR" and instrument.kind != "stock":
        # ETF/ETN 등은 주식과 호가단위 규칙이 다르고 정확한 표를 확인하지 못했다.
        # 잘못 거부하지 않도록 원 단위만 검사하고 최종 판단은 KIS 에 맡긴다.  VERIFY
        return Decimal("1")
    if instrument.market == "KR":
        for upper, tick in _KR_TICK_BANDS:
            if price < upper:
                return tick
        return _KR_TICK_TOP
    return Decimal("0.01") if price >= 1 else Decimal("0.0001")


def is_tick_aligned(instrument: Instrument, price: Decimal) -> bool:
    return price > 0 and price % tick_size(instrument, price) == 0


class InstrumentRegistry:
    def __init__(self, instruments: list[Instrument] | None = None) -> None:
        self._items: dict[str, Instrument] = {}
        for i in instruments or []:
            self.add(i)

    def add(self, instrument: Instrument) -> None:
        self._items[instrument.id] = instrument

    def get(self, instrument_id: str) -> Instrument | None:
        return self._items.get(instrument_id)

    def ensure(self, instrument_id: str, exchange: str | None = None) -> Instrument:
        """마스터에 없는 종목(HTS 수동매매 등)을 최소 정보로 등록해 반환."""
        found = self._items.get(instrument_id)
        if found is not None:
            return found
        market, _, code = instrument_id.partition(":")
        if market not in MARKET_CURRENCY or not code:
            raise ValueError(f"잘못된 종목 식별자: {instrument_id}")
        inst = Instrument(instrument_id, market, code, exchange or ("KRX" if market == "KR" else "NASD"))  # type: ignore[arg-type]
        self.add(inst)
        return inst
