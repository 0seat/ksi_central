"""KIS 엔드포인트와 TR ID 테이블 (단일 관리 지점).

출처: koreainvestment/open-trading-api 의 examples_llm/<분류>/<api>/<api>.py
모의/실전 차이는 이 테이블의 real/paper 값으로만 표현한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from central.settings import Env

REST_BASE: dict[Env, str] = {
    "real": "https://openapi.koreainvestment.com:9443",
    "paper": "https://openapivts.koreainvestment.com:29443",
}
WS_BASE: dict[Env, str] = {
    "real": "ws://ops.koreainvestment.com:21000",
    "paper": "ws://ops.koreainvestment.com:31000",
}
WS_PATH = "/tryitout"

Method = Literal["GET", "POST"]


@dataclass(frozen=True)
class Tr:
    path: str
    method: Method
    real: str
    paper: str | None  # None: 모의투자 미지원으로 확인된 TR

    def id(self, env: Env) -> str:
        tr_id = self.real if env == "real" else self.paper
        if tr_id is None:
            raise ValueError(f"{self.path}: {env} 환경에서 지원하지 않는 TR")
        return tr_id


def _same(path: str, tr_id: str, method: Method = "GET") -> Tr:
    return Tr(path, method, tr_id, tr_id)


# ───────────── 국내주식 ─────────────
_DS = "/uapi/domestic-stock/v1"
KR_PRICE = _same(f"{_DS}/quotations/inquire-price", "FHKST01010100")
KR_DAILY_CHART = _same(f"{_DS}/quotations/inquire-daily-itemchartprice", "FHKST03010100")
KR_MINUTE_TODAY = _same(f"{_DS}/quotations/inquire-time-itemchartprice", "FHKST03010200")
KR_MINUTE_DAILY = _same(f"{_DS}/quotations/inquire-time-dailychartprice", "FHKST03010230")
KR_ASKING_PRICE = _same(f"{_DS}/quotations/inquire-asking-price-exp-ccn", "FHKST01010200")  # 10단계 호가
KR_HOLIDAY = Tr(f"{_DS}/quotations/chk-holiday", "GET", "CTCA0903R", None)  # 모의: EGW02006 (2026-10-13 확인)
KR_BALANCE = Tr(f"{_DS}/trading/inquire-balance", "GET", "TTTC8434R", "VTTC8434R")
KR_PSBL_ORDER = Tr(f"{_DS}/trading/inquire-psbl-order", "GET", "TTTC8908R", "VTTC8908R")
KR_INTGR_MARGIN = Tr(f"{_DS}/trading/intgr-margin", "GET", "TTTC0869R", None)
KR_ORDER_BUY = Tr(f"{_DS}/trading/order-cash", "POST", "TTTC0012U", "VTTC0012U")
KR_ORDER_SELL = Tr(f"{_DS}/trading/order-cash", "POST", "TTTC0011U", "VTTC0011U")
KR_ORDER_RVSECNCL = Tr(f"{_DS}/trading/order-rvsecncl", "POST", "TTTC0013U", "VTTC0013U")
KR_DAILY_CCLD = Tr(f"{_DS}/trading/inquire-daily-ccld", "GET", "TTTC0081R", "VTTC0081R")  # 3개월 이내

# ───────────── 해외주식 (미국) ─────────────
_OP = "/uapi/overseas-price/v1"
_OS = "/uapi/overseas-stock/v1"
# 지수 (2026-10-15 실측: 일봉 국내 50개/해외 100개씩, 분봉은 최근 약 100개만)
KR_INDEX_DAILY = _same(f"{_DS}/quotations/inquire-daily-indexchartprice", "FHKUP03500100")
KR_INDEX_MINUTE = _same(f"{_DS}/quotations/inquire-time-indexchartprice", "FHKUP03500200")
OVERSEAS_INDEX_DAILY = _same(f"{_OP}/quotations/inquire-daily-chartprice", "FHKST03030100")
OVERSEAS_INDEX_MINUTE = _same(f"{_OP}/quotations/inquire-time-indexchartprice", "FHKST03030200")
# 시가총액 순위 (해외는 공식 예제에 없는 CURR_GB 가 필수, 2026-10-15 실측)
KR_MARKET_CAP = _same(f"{_DS}/ranking/market-cap", "FHPST01740000")
US_MARKET_CAP = _same(f"{_OS}/ranking/market-cap", "HHDFS76350100")

US_PRICE = _same(f"{_OP}/quotations/price", "HHDFS00000300")
US_DAILY_CHART = _same(f"{_OP}/quotations/dailyprice", "HHDFS76240000")
US_MINUTE_CHART = _same(f"{_OP}/quotations/inquire-time-itemchartprice", "HHDFS76950200")
US_ASKING_PRICE = _same(f"{_OP}/quotations/inquire-asking-price", "HHDFS76200100")  # 10단계 (미국 무료는 1호가)
US_BALANCE = Tr(f"{_OS}/trading/inquire-balance", "GET", "TTTS3012R", "VTTS3012R")
US_PRESENT_BALANCE = Tr(f"{_OS}/trading/inquire-present-balance", "GET", "CTRP6504R", "VTRP6504R")
US_PSAMOUNT = Tr(f"{_OS}/trading/inquire-psamount", "GET", "TTTS3007R", "VTTS3007R")
US_FOREIGN_MARGIN = Tr(f"{_OS}/trading/foreign-margin", "GET", "TTTC2101R", None)
US_ORDER_BUY = Tr(f"{_OS}/trading/order", "POST", "TTTT1002U", "VTTT1002U")
US_ORDER_SELL = Tr(f"{_OS}/trading/order", "POST", "TTTT1006U", "VTTT1006U")
US_ORDER_RVSECNCL = Tr(f"{_OS}/trading/order-rvsecncl", "POST", "TTTT1004U", "VTTT1004U")
US_CCNL = Tr(f"{_OS}/trading/inquire-ccnl", "GET", "TTTS3035R", "VTTS3035R")

# ───────────── 실시간 (WebSocket) ─────────────
# 시세 TR은 모의/실전 동일, 체결통보만 다르다. 체결통보 tr_key 는 HTS ID.
WS_KR_TICK_KRX = "H0STCNT0"
WS_KR_TICK_NXT = "H0NXCNT0"
WS_KR_TICK_UNIFIED = "H0UNCNT0"
WS_KR_ORDERBOOK_KRX = "H0STASP0"
WS_KR_ORDERBOOK_NXT = "H0NXASP0"
WS_KR_ORDERBOOK_UNIFIED = "H0UNASP0"
WS_US_TICK = "HDFSCNT0"        # 미국: 0분 지연(실시간) 무료
WS_US_ORDERBOOK = "HDFSASP0"   # 미국: 1호가 실시간 무료
WS_KR_NOTICE: dict[Env, str] = {"real": "H0STCNI0", "paper": "H0STCNI9"}
WS_US_NOTICE: dict[Env, str] = {"real": "H0GSCNI0", "paper": "H0GSCNI9"}

# 미국 거래소 코드: 시세 API(EXCD) / 주문·잔고 API(OVRS_EXCG_CD)
US_QUOTE_EXCD = {"NASD": "NAS", "NYSE": "NYS", "AMEX": "AMS"}


def us_ws_key(exchange: str, symbol: str) -> str:
    """미국 실시간 tr_key. 예: ("NASD", "AAPL") -> "DNASAAPL" """
    return f"D{US_QUOTE_EXCD[exchange]}{symbol}"
