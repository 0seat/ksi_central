"""통화와 금액 계산. 금액은 항상 Decimal 이며 float 는 받지 않는다."""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal
from typing import Literal

Currency = Literal["KRW", "USD"]
CURRENCIES: tuple[Currency, ...] = ("KRW", "USD")

ZERO = Decimal("0")
_CASH_QUANT: dict[str, Decimal] = {"KRW": Decimal("1"), "USD": Decimal("0.01")}
PRICE_QUANT = Decimal("0.0001")


def D(value: str | int | Decimal) -> Decimal:
    if isinstance(value, float):
        raise TypeError("금액에 float 사용 금지: 문자열이나 Decimal 을 쓸 것")
    return value if isinstance(value, Decimal) else Decimal(str(value))


def q_cash(amount: Decimal, currency: str) -> Decimal:
    """일반 금액 (체결대금 등): 반올림."""
    return amount.quantize(_CASH_QUANT[currency], rounding=ROUND_HALF_UP)


def q_hold(amount: Decimal, currency: str) -> Decimal:
    """선점 금액: 보수적으로 올림."""
    return amount.quantize(_CASH_QUANT[currency], rounding=ROUND_UP)


def q_fee(amount: Decimal, currency: str) -> Decimal:
    """수수료/세금: 원화는 원 미만 절사, 외화는 센트 반올림."""
    rounding = ROUND_DOWN if currency == "KRW" else ROUND_HALF_UP
    return amount.quantize(_CASH_QUANT[currency], rounding=rounding)


def q_price(price: Decimal) -> Decimal:
    return price.quantize(PRICE_QUANT, rounding=ROUND_HALF_UP)
