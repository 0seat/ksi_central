"""실시간 시세 메시지 해석 (체결가, 호가).

컬럼 순서는 공식 샘플(examples_user/*_functions_ws.py) 기준.
- 국내 체결 H0STCNT0(KRX) / H0UNCNT0(통합) / H0NXCNT0(NXT): 47개 필드, 앞부분 동일 (2026-10-14 실측 47개)
- 국내 호가 H0STASP0 / H0UNASP0 / H0NXASP0: 앞 43개(종목, 시간, 10단계 호가와 잔량) + 총잔량 2개는 동일
- 미국 체결 HDFSCNT0: 맨 앞에 RSYM(예: DNASAAPL)이 붙어 26개 (샘플 25개, 2026-10-13 실측)
- 미국 호가 HDFSASP0: 실측 71개 = RSYM + 헤더 10개 + 10단계 x 6개로 추정.
  미국은 1호가만 무료라 1호가만 신뢰한다  VERIFY (미국장 실데이터로 확인)

주의: 국내 체결가는 호가단위에 맞지 않을 수 있다. KRX 중간가 호가는 최우선 매수/매도호가의 한가운데에서
체결된다 (2026-10-14 실측: 매수 234,500 / 매도 235,000 에서 234,750 체결). 호가 가격은 호가단위를 따른다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from central.core.market_hours import KST

KR_TICK_COLUMNS = [
    "MKSC_SHRN_ISCD", "STCK_CNTG_HOUR", "STCK_PRPR", "PRDY_VRSS_SIGN", "PRDY_VRSS", "PRDY_CTRT",
    "WGHN_AVRG_STCK_PRC", "STCK_OPRC", "STCK_HGPR", "STCK_LWPR", "ASKP1", "BIDP1", "CNTG_VOL", "ACML_VOL",
    "ACML_TR_PBMN", "SELN_CNTG_CSNU", "SHNU_CNTG_CSNU", "NTBY_CNTG_CSNU", "CTTR", "SELN_CNTG_SMTN",
    "SHNU_CNTG_SMTN", "CCLD_DVSN", "SHNU_RATE", "PRDY_VOL_VRSS_ACML_VOL_RATE", "OPRC_HOUR",
    "OPRC_VRSS_PRPR_SIGN", "OPRC_VRSS_PRPR", "HGPR_HOUR", "HGPR_VRSS_PRPR_SIGN", "HGPR_VRSS_PRPR",
    "LWPR_HOUR", "LWPR_VRSS_PRPR_SIGN", "LWPR_VRSS_PRPR", "BSOP_DATE", "NEW_MKOP_CLS_CODE", "TRHT_YN",
    "ASKP_RSQN1", "BIDP_RSQN1", "TOTAL_ASKP_RSQN", "TOTAL_BIDP_RSQN", "VOL_TNRT", "PRDY_SMNS_HOUR_ACML_VOL",
    "PRDY_SMNS_HOUR_ACML_VOL_RATE", "HOUR_CLS_CODE", "MRKT_TRTM_CLS_CODE", "VI_STND_PRC", "MARKET_CLS_CODE",
]
_KT = {name: i for i, name in enumerate(KR_TICK_COLUMNS)}

US_TICK_COLUMNS = [
    "SYMB", "ZDIV", "TYMD", "XYMD", "XHMS", "KYMD", "KHMS", "OPEN", "HIGH", "LOW", "LAST", "SIGN", "DIFF",
    "RATE", "PBID", "PASK", "VBID", "VASK", "EVOL", "TVOL", "TAMT", "BIVL", "ASVL", "STRN", "MTYP",
]
_UT = {name: i for i, name in enumerate(US_TICK_COLUMNS)}
_US_BOOK_HEADER = 10  # symb zdiv xymd xhms kymd khms bvol avol bdvl advl
_US_BOOK_LEVEL = 6    # pbid pask vbid vask dbid dask

KR_TICK_TRS = {"H0STCNT0": "KRX", "H0UNCNT0": "UNIFIED", "H0NXCNT0": "NXT"}
KR_BOOK_TRS = {"H0STASP0": "KRX", "H0UNASP0": "UNIFIED", "H0NXASP0": "NXT"}
US_TICK_TR = "HDFSCNT0"
US_BOOK_TR = "HDFSASP0"


@dataclass(frozen=True)
class Tick:
    symbol: str
    ts: datetime
    price: Decimal
    qty: int
    cum_volume: int
    bid: Decimal | None
    ask: Decimal | None
    side: str | None     # buy / sell (체결 방향, 알 수 없으면 None)
    venue: str           # KRX / NXT / UNIFIED / US


@dataclass(frozen=True)
class Book:
    symbol: str
    ts: datetime
    asks: list[tuple[Decimal, int]]   # 가까운 호가부터
    bids: list[tuple[Decimal, int]]
    venue: str


def _dec(v: str) -> Decimal | None:
    try:
        d = Decimal(v.strip())
    except (InvalidOperation, AttributeError):
        return None
    return d


def _int(v: str) -> int:
    d = _dec(v)
    return int(d) if d is not None else 0


def _ts(ymd: str, hms: str, fallback: date) -> datetime:
    ymd, hms = ymd.strip(), hms.strip()[:6].ljust(6, "0")
    try:
        day = datetime.strptime(ymd, "%Y%m%d").date() if ymd else fallback
        return datetime.combine(day, datetime.strptime(hms, "%H%M%S").time(), KST)
    except ValueError:
        return datetime.combine(fallback, datetime.min.time(), KST)


def parse_kr_tick(tr_id: str, rec: list[str], today: date) -> Tick | None:
    if len(rec) < _KT["BSOP_DATE"] + 1:
        return None
    price = _dec(rec[_KT["STCK_PRPR"]])
    if not price:
        return None
    side = {"1": "buy", "5": "sell"}.get(rec[_KT["CCLD_DVSN"]].strip())
    return Tick(
        symbol=f"KR:{rec[_KT['MKSC_SHRN_ISCD']].strip()}",
        ts=_ts(rec[_KT["BSOP_DATE"]], rec[_KT["STCK_CNTG_HOUR"]], today),
        price=price, qty=_int(rec[_KT["CNTG_VOL"]]), cum_volume=_int(rec[_KT["ACML_VOL"]]),
        bid=_dec(rec[_KT["BIDP1"]]) or None, ask=_dec(rec[_KT["ASKP1"]]) or None, side=side,
        venue=KR_TICK_TRS.get(tr_id, "KRX"),
    )


def parse_kr_book(tr_id: str, rec: list[str], today: date) -> Book | None:
    if len(rec) < 43:
        return None
    asks = [(_dec(rec[3 + i]), _int(rec[23 + i])) for i in range(10)]
    bids = [(_dec(rec[13 + i]), _int(rec[33 + i])) for i in range(10)]
    return Book(
        symbol=f"KR:{rec[0].strip()}", ts=_ts("", rec[1], today),
        asks=[(p, q) for p, q in asks if p], bids=[(p, q) for p, q in bids if p],  # type: ignore[misc]
        venue=KR_BOOK_TRS.get(tr_id, "KRX"),
    )


def _us_offset(rec: list[str], sample_len: int) -> int:
    """실데이터는 맨 앞에 RSYM(D+거래소+티커)이 붙는다. 샘플 길이와 비교해 판단."""
    return 1 if len(rec) > sample_len and rec[0].startswith(("D", "R")) and rec[0].endswith(rec[1].strip()) else 0


def parse_us_tick(rec: list[str], today: date) -> Tick | None:
    o = _us_offset(rec, len(US_TICK_COLUMNS))
    if len(rec) < o + len(US_TICK_COLUMNS):
        return None
    f = lambda name: rec[o + _UT[name]]  # noqa: E731
    price = _dec(f("LAST"))
    if not price:
        return None
    return Tick(
        symbol=f"US:{f('SYMB').strip()}", ts=_ts(f("KYMD"), f("KHMS"), today), price=price,
        qty=_int(f("EVOL")), cum_volume=_int(f("TVOL")), bid=_dec(f("PBID")) or None, ask=_dec(f("PASK")) or None,
        side=None, venue="US",
    )


def parse_us_book(rec: list[str], today: date) -> Book | None:
    o = _us_offset(rec, _US_BOOK_HEADER + _US_BOOK_LEVEL)
    base = o + _US_BOOK_HEADER
    if len(rec) < base + _US_BOOK_LEVEL:
        return None
    asks, bids = [], []
    for lv in range((len(rec) - base) // _US_BOOK_LEVEL):
        pbid, pask, vbid, vask = rec[base + lv * 6: base + lv * 6 + 4]
        if _dec(pbid):
            bids.append((_dec(pbid), _int(vbid)))
        if _dec(pask):
            asks.append((_dec(pask), _int(vask)))
    return Book(symbol=f"US:{rec[o].strip()}", ts=_ts(rec[o + 4], rec[o + 5], today),
                asks=asks, bids=bids, venue="US")  # type: ignore[arg-type]
