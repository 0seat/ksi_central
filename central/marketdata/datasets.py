"""시세 부가 데이터셋: 순위(/market/rankings)와 수급(/market/flows).

여기서는 브로커와 무관한 조건 정의(종류, 조건 이름, 허용값, 단위)와 짧은 캐시를 맡는다.
KIS 요청 파라미터와 응답 변환은 central/broker/kis/datasets.py 에 종류별로 있다.
여러 앱이 같은 조건으로 부르면 TTL 동안 KIS 를 한 번만 부른다 (동시에 들어온 요청도 한 번으로 합친다).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from central.broker.base import Broker, BrokerUnavailable
from central.oms.errors import OmsError

TTL_SEC = 10
MAX_CACHE = 300

# KIS 대상 제외 구분 코드 10자리의 순서 (국내 거래량/등락률/체결강도 순위)
EXCLUDES = ("caution", "managed", "liquidation", "unfaithful", "preferred", "halted", "etf", "etn",
            "no_credit", "spac")
US_MINUTES = (1, 2, 3, 5, 10, 15, 20, 30, 60, 120)
US_DAYS = (1, 2, 3, 5, 10, 20, 30, 60, 120, 250)       # 1 = 당일, 250 = 1년
US_VOLUME_STEPS = (0, 100, 1_000, 10_000, 100_000, 1_000_000, 10_000_000)


@dataclass(frozen=True)
class Opt:
    name: str
    type: str = "choice"            # choice | int | dec | list | date | symbol
    choices: tuple = ()
    default: Any = None
    required: bool = False

    def to_json(self) -> dict:
        d: dict[str, Any] = {"name": self.name, "type": self.type}
        if self.choices:
            d["choices"] = list(self.choices)
        if self.default not in (None, ()):
            d["default"] = self.default
        if self.required:
            d["required"] = True
        return d


@dataclass(frozen=True)
class Spec:
    group: str                      # rankings | flows
    kind: str
    market: str                     # KR | US (어느 실계좌로 부를지)
    doc: str
    opts: tuple[Opt, ...] = ()
    units: tuple[tuple[str, str], ...] = ()

    def to_json(self) -> dict:
        return {"kind": self.kind, "market": self.market, "doc": self.doc,
                "options": [o.to_json() for o in self.opts], "units": dict(self.units)}


def _sort(*choices: str) -> Opt:
    return Opt("sort", choices=choices, default=choices[0])


UNIVERSE = Opt("universe", choices=("all", "kospi", "kosdaq", "kospi200"), default="all")
UNIVERSE3 = Opt("universe", choices=("all", "kospi", "kosdaq"), default="all")
STOCK_TYPE = Opt("stock_type", choices=("all", "common", "preferred"), default="all")
EXCLUDE = Opt("exclude", "list", choices=EXCLUDES, default=())
PRICE = (Opt("min_price", "dec"), Opt("max_price", "dec"))
MIN_VOLUME = Opt("min_volume", "int")
KR_FILTERS = (UNIVERSE, *PRICE, MIN_VOLUME)
US_EXCHANGE = Opt("exchange", choices=("NAS", "NYS", "AMS"), default="NAS")
US_BASE = (US_EXCHANGE, MIN_VOLUME)
MINUTES = Opt("minutes", "int", choices=US_MINUTES, default=5)
DAYS = Opt("days", "int", choices=US_DAYS, default=1)
SYMBOL = Opt("symbol", "symbol", required=True)
DATE = Opt("date", "date")
RANGE = (Opt("start", "date"), Opt("end", "date"))
KR_MARKET = Opt("market", choices=("kospi", "kosdaq"), default="kospi")

_R, _F = "rankings", "flows"
SPECS: dict[tuple[str, str], Spec] = {(s.group, s.kind): s for s in [
    # ───────────── 국내 순위 (최대 30행, 신용잔고는 100행) ─────────────
    Spec(_R, "kr_volume", "KR", "거래량 순위", (
        _sort("volume", "volume_growth", "volume_turnover", "value", "value_turnover"),
        STOCK_TYPE, EXCLUDE, *KR_FILTERS), (("value", "KRW"),)),
    Spec(_R, "kr_fluctuation", "KR", "등락률 순위 (전일 종가 기준). range 는 당일 고저 폭",
         (_sort("up", "down", "open_up", "open_down", "range"), EXCLUDE, *KR_FILTERS,
          Opt("min_rate", "dec"), Opt("max_rate", "dec"))),
    Spec(_R, "kr_volume_power", "KR", "체결강도 순위", (STOCK_TYPE, EXCLUDE, *KR_FILTERS)),
    Spec(_R, "kr_near_high_low", "KR", "신고가(high)/신저가(low) 근접. max_gap 은 근접 비율 상한 %",
         (_sort("high", "low"), Opt("max_gap", "dec"), *KR_FILTERS)),
    Spec(_R, "kr_disparity", "KR", "이격도 순위 (현재가 / N일 이동평균 x 100)",
         (_sort("high", "low"), Opt("period", "int", (5, 10, 20, 60, 120), 20), *KR_FILTERS)),
    Spec(_R, "kr_expected", "KR", "예상체결 순위. session=open 장전, close 장마감. last 는 예상체결가",
         (_sort("up", "up_amount", "flat", "down", "down_amount", "volume", "value"),
          Opt("session", choices=("open", "close"), default="open"), STOCK_TYPE, UNIVERSE,
          Opt("min_price", "dec"), MIN_VOLUME), (("expected_value", "KRW"),)),
    Spec(_R, "kr_order_imbalance", "KR", "호가잔량 순위 (순매수잔량, 순매도잔량, 매수비율, 매도비율)",
         (_sort("net_buy", "net_sell", "buy_ratio", "sell_ratio"), *KR_FILTERS)),
    Spec(_R, "kr_short_sale", "KR", "공매도 상위 (기간 합계)",
         (Opt("period", choices=("1d", "2d", "3d", "4d", "1w", "2w", "3w", "1m", "2m", "3m"), default="1d"),
          *KR_FILTERS), (("value", "KRW"), ("short_value", "KRW"))),
    Spec(_R, "kr_credit", "KR", "신용잔고 상위. margin_* 융자(신용매수), stock_loan_* 대주(신용매도). "
         "*_up/*_down 은 days 일 전 대비 잔고비율 증감",
         (_sort("margin_ratio", "margin_qty", "margin_amount", "margin_ratio_up", "margin_ratio_down",
                "stock_loan_ratio", "stock_loan_qty", "stock_loan_amount", "stock_loan_ratio_up",
                "stock_loan_ratio_down"), Opt("days", "int", default=2), UNIVERSE),
         (("margin_amount", "KIS 원본 (만원 추정, 미확인)"), ("stock_loan_amount", "KIS 원본 (만원 추정, 미확인)"))),
    Spec(_R, "kr_interest", "KR", "관심종목 등록 상위", KR_FILTERS, (("value", "KRW"),)),
    Spec(_R, "kr_hts_views", "KR", "HTS 조회 상위 20 (종목코드만)"),
    Spec(_R, "kr_market_cap", "KR", "시가총액 상위", (STOCK_TYPE, *KR_FILTERS), (("market_cap", "KRW 억원"),)),
    Spec(_R, "kr_limit", "KR", "상한가(upper)/하한가(lower) 종목. near 를 주면 그 % 이내 근접 종목",
         (_sort("upper", "lower"), Opt("near", choices=("limit", "8", "10", "15", "20", "25"), default="limit"),
          UNIVERSE3, *PRICE, MIN_VOLUME)),
    Spec(_R, "kr_investor_net", "KR", "외국인/기관 순매수(buy)/순매도(sell) 상위 (장중 가집계)",
         (_sort("buy", "sell"), Opt("investor", choices=("all", "foreign", "institution", "other"), default="all"),
          Opt("by", choices=("qty", "value"), default="value"), UNIVERSE3),
         (("investors.*.net_value", "KRW 백만원"),)),
    Spec(_R, "kr_foreign_broker", "KR", "외국계 증권사 창구 매수(buy)/매도(sell) 상위 (가집계)",
         (_sort("buy", "sell"), Opt("by", choices=("value", "qty"), default="value"), UNIVERSE)),
    # ───────────── 미국 순위 (100행) ─────────────
    Spec(_R, "us_volume_surge", "US", "minutes 분 전 대비 거래량 급증", (*US_BASE, MINUTES)),
    Spec(_R, "us_price_fluct", "US", "minutes 분 전 대비 가격 급등(up)/급락(down)", (_sort("up", "down"), *US_BASE, MINUTES)),
    Spec(_R, "us_updown", "US", "상승률(up)/하락률(down). days 일 전 대비 (1 = 당일)", (_sort("up", "down"), *US_BASE, DAYS)),
    Spec(_R, "us_new_high_low", "US", "신고가(high)/신저가(low). mode=hold 돌파 유지, touch 일시 돌파",
         (_sort("high", "low"), Opt("mode", choices=("hold", "touch"), default="hold"), *US_BASE, MINUTES)),
    Spec(_R, "us_volume", "US", "거래량 순위", (*US_BASE, DAYS, *PRICE), (("value", "USD"),)),
    Spec(_R, "us_value", "US", "거래대금 순위", (*US_BASE, DAYS, *PRICE), (("value", "USD"),)),
    Spec(_R, "us_volume_growth", "US", "거래증가율 순위", (*US_BASE, DAYS)),
    Spec(_R, "us_turnover", "US", "거래회전율 순위", (*US_BASE, DAYS)),
    Spec(_R, "us_volume_power", "US", "매수체결강도 상위", (*US_BASE, DAYS)),
    Spec(_R, "us_market_cap", "US", "시가총액 상위", US_BASE, (("market_cap", "USD"),)),
    # ───────────── 국내 수급 (종목) ─────────────
    Spec(_F, "investor_daily", "KR", "종목 투자자별 일별 매매 (date 까지 30일, 생략하면 최근)", (SYMBOL, DATE),
         (("value", "KRW"), ("investors.*.*_value", "KRW 백만원"))),
    Spec(_F, "investor_estimate", "KR", "종목 외국인/기관 장중 추정 순매수 (가집계). slot 은 KIS 입력구분 코드(1~5)",
         (SYMBOL,)),
    Spec(_F, "program", "KR", "종목 프로그램 매매 장중 추이 (최근 30건)", (SYMBOL,), (("*_value", "KRW"),)),
    Spec(_F, "program_daily", "KR", "종목 프로그램 매매 일별 (date 까지 30일)", (SYMBOL, DATE),
         (("value", "KRW"), ("*_value", "KRW"))),
    Spec(_F, "foreign_broker", "KR", "종목 외국계 증권사 창구 순매수 장중 추이", (SYMBOL,)),
    Spec(_F, "members", "KR", "종목 매도/매수 상위 5개 증권사 창구와 외국계 합계", (SYMBOL,)),
    Spec(_F, "buy_sell_daily", "KR", "종목 일별 매수/매도 체결량", (SYMBOL, *RANGE)),
    Spec(_F, "short_sale_daily", "KR", "종목 공매도 일별", (SYMBOL, *RANGE), (("*value", "KRW"),)),
    Spec(_F, "credit_daily", "KR", "종목 신용잔고 일별 (date 결제일 기준 30건)", (SYMBOL, DATE),
         (("*_amount", "KIS 원본 (만원 추정, 미확인)"),)),
    Spec(_F, "stock_loan_daily", "KR", "종목 대차거래 일별", (SYMBOL, *RANGE), (("balance_amount", "KRW 백만원"),)),
    # ───────────── 국내 수급 (시장) ─────────────
    Spec(_F, "market_investor_daily", "KR", "시장 투자자별 일별 매매 (date 까지)", (KR_MARKET, DATE),
         (("investors.*.net_value", "KRW 백만원"), ("investors.*.net_qty", "천주 (추정)"))),
    Spec(_F, "market_program", "KR", "시장 프로그램 매매 장중 추이", (KR_MARKET,), (("*_value", "KRW 백만원"),)),
    Spec(_F, "market_program_daily", "KR", "시장 프로그램 매매 일별", (KR_MARKET, *RANGE), (("*_value", "KRW 백만원"),)),
    Spec(_F, "market_program_investor", "KR", "시장 프로그램 매매 투자자별 (당일)", (KR_MARKET,),
         (("*_value", "KRW 백만원"),)),
    Spec(_F, "market_funds", "KR", "증시자금 일별 (고객예탁금, 신용융자, 미수금, 펀드)", (DATE,), (("*", "KRW 억원"),)),
]}


@dataclass(frozen=True)
class DatasetQuery:
    group: str
    kind: str
    opts: tuple[tuple[str, Any], ...] = ()

    @property
    def spec(self) -> Spec:
        return SPECS[(self.group, self.kind)]

    @property
    def market(self) -> str:
        return self.spec.market

    def get(self, name: str, default: Any = None) -> Any:
        for k, v in self.opts:
            if k == name:
                return v
        return default

    @property
    def code(self) -> str:
        """symbol 옵션의 종목코드 (KR:005930 → 005930)."""
        return str(self.get("symbol", "")).split(":", 1)[-1]

    @property
    def us_volume_step(self) -> int:
        """KIS 미국 순위의 거래량 조건은 단계값만 받는다: min_volume 이하의 가장 큰 단계 (나머지는 행 필터)."""
        return max(i for i, v in enumerate(US_VOLUME_STEPS) if v <= (self.get("min_volume") or 0))

    def to_json(self) -> dict:
        return {k: (list(v) if isinstance(v, tuple) else v) for k, v in self.opts}


def kinds(group: str) -> list[dict]:
    return [s.to_json() for (g, _), s in SPECS.items() if g == group]


def build_query(group: str, kind: str, raw: dict[str, str], today: date) -> DatasetQuery:
    """API 쿼리 문자열을 검사해 DatasetQuery 로. 잘못된 값은 INVALID_REQUEST. 미래 날짜는 거부한다."""
    def bad(msg: str) -> OmsError:
        return OmsError("INVALID_REQUEST", msg)

    spec = SPECS.get((group, kind))
    if spec is None:
        raise bad(f"{group} 종류는 {', '.join(k for g, k in SPECS if g == group)} 중 하나")
    known = {o.name: o for o in spec.opts}
    unknown = sorted(set(raw) - set(known))
    if unknown:
        raise bad(f"{kind} 는 {', '.join(unknown)} 를 받지 않음 (가능: {', '.join(known) or '없음'})")
    out: list[tuple[str, Any]] = []
    for o in spec.opts:
        v = raw.get(o.name)
        if v is None or v == "":
            if o.required:
                raise bad(f"{o.name} 필요")
            if o.default not in (None, ()):
                out.append((o.name, o.default))
            continue   # 날짜를 비우면 KIS 가 최근 거래일 기준으로 준다 (오늘 날짜는 장 마감 전후로 거부되는 TR 이 있다)
        x = _convert(o, v, bad)
        if o.type == "date" and x > today:
            raise bad(f"{o.name} 는 오늘({today}) 이전이어야 함")
        out.append((o.name, x))
    return DatasetQuery(group, kind, tuple(out))


def _convert(o: Opt, v: str, bad: Callable[[str], OmsError]) -> Any:
    try:
        if o.type == "int":
            x: Any = int(v)
            if x < 0:
                raise ValueError
        elif o.type == "dec":
            x = Decimal(v)
        elif o.type == "date":
            x = date.fromisoformat(v if "-" in v else f"{v[:4]}-{v[4:6]}-{v[6:8]}")
        elif o.type == "list":
            x = tuple(s.strip() for s in v.split(",") if s.strip())
            if any(s not in o.choices for s in x):
                raise ValueError
            return x
        elif o.type == "symbol":
            x = v.strip().upper()
            if not x.startswith("KR:") or len(x) < 4:
                raise ValueError
            return x
        else:
            x = v
    except (ValueError, InvalidOperation):
        hint = f": {', '.join(map(str, o.choices))}" if o.choices else ""
        raise bad(f"{o.name} 값이 올바르지 않음 ({o.type}{hint})") from None
    if o.choices and x not in o.choices:
        raise bad(f"{o.name} 는 {', '.join(map(str, o.choices))} 중 하나")
    return x


@dataclass
class _Entry:
    at: float
    ts: datetime
    rows: list[dict] = field(default_factory=list)


class DatasetService:
    def __init__(self, broker_for: Callable[[str], Broker], clock: Callable[[], datetime],
                 ttl: float = TTL_SEC) -> None:
        self.broker_for = broker_for
        self.clock = clock
        self.ttl = ttl
        self._cache: dict[DatasetQuery, _Entry] = {}
        self._inflight: dict[DatasetQuery, asyncio.Future] = {}

    def query(self, group: str, kind: str, raw: dict[str, str]) -> DatasetQuery:
        return build_query(group, kind, raw, self.clock().date())

    async def get(self, q: DatasetQuery) -> tuple[list[dict], datetime, bool]:
        """(행, 조회 시각, 캐시 여부)."""
        hit = self._cache.get(q)
        if hit is not None and time.monotonic() - hit.at < self.ttl:
            return hit.rows, hit.ts, True
        if q in self._inflight:
            entry = await asyncio.shield(self._inflight[q])
            return entry.rows, entry.ts, True
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[q] = fut
        try:
            rows = await self.broker_for(q.market).dataset(q)
            min_volume = q.get("min_volume")
            if min_volume:
                rows = [r for r in rows if r.get("volume", 0) >= min_volume]
            entry = _Entry(time.monotonic(), self.clock(), rows)
            if len(self._cache) >= MAX_CACHE:
                self._cache.clear()
            self._cache[q] = entry
            fut.set_result(entry)
            return rows, entry.ts, False
        except BrokerUnavailable as e:
            err = OmsError("BROKER_UNAVAILABLE", str(e))
            fut.set_exception(err)
            fut.exception()   # 기다리는 쪽이 없어도 경고가 나지 않게
            raise err from None
        except asyncio.CancelledError:
            fut.cancel()
            raise
        except Exception as e:
            fut.set_exception(e)
            fut.exception()
            raise
        finally:
            del self._inflight[q]
