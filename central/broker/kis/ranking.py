"""KIS 순위 API 요청 파라미터와 응답 행 변환 (central/marketdata/rankings.py 의 RankingQuery).

파라미터는 공식 예제(examples_llm/domestic_stock/{volume_rank,fluctuation,volume_power},
overseas_stock/{volume_surge,price_fluct})를 따르되, 2026-10-17 실측으로 고친 것이 있다.
- 등락률 순위 fid_rank_sort_cls_code 는 한 자리 (예제의 "0000" 은 OPSQ2002 길이 오류).
  0 상승률, 1 하락률, 2 시가 대비 상승, 3 시가 대비 하락, 4 고저 변동폭
- 등락률 순위 fid_prc_cls_code 0 은 저가 대비, 1 은 전일 종가 대비로 정렬된다. 1 을 쓴다.
- 대상 제외 10자리 코드는 세 국내 순위 모두 받는다.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from central.broker.kis import tr
from central.marketdata.rankings import EXCLUDES, SORTS, US_MINUTES, RankingQuery

_UNIVERSE = {"all": "0000", "kospi": "0001", "kosdaq": "1001", "kospi200": "2001"}
_STOCK_TYPE = {"all": "0", "common": "1", "preferred": "2"}
US_MINUTE_INDEX = {m: i for i, m in enumerate(US_MINUTES)}


def _num(v: Decimal | None) -> str:
    return "" if v is None else format(v, "f")


def request(q: RankingQuery) -> tuple[tr.Tr, dict[str, str], str]:
    """(TR, 파라미터, 행이 담긴 응답 키)."""
    if q.market == "KR":
        excl = "".join("1" if x in q.exclude else "0" for x in EXCLUDES)
        common = {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": _UNIVERSE[q.universe],
                  "fid_input_price_1": _num(q.min_price), "fid_input_price_2": _num(q.max_price),
                  "fid_vol_cnt": str(q.min_volume or ""), "fid_trgt_exls_cls_code": excl}
        if q.kind == "kr_volume":
            p = {**common, "fid_cond_scr_div_code": "20171", "fid_div_cls_code": _STOCK_TYPE[q.stock_type],
                 "fid_blng_cls_code": str(SORTS["kr_volume"].index(q.sort)), "fid_trgt_cls_code": "111111111",
                 "fid_input_date_1": ""}
            return tr.KR_VOLUME_RANK, {k.upper(): v for k, v in p.items()}, "output"
        if q.kind == "kr_fluctuation":
            return tr.KR_FLUCTUATION, {
                **common, "fid_cond_scr_div_code": "20170",
                "fid_rank_sort_cls_code": str(SORTS["kr_fluctuation"].index(q.sort)), "fid_input_cnt_1": "0",
                "fid_prc_cls_code": "1", "fid_trgt_cls_code": "0", "fid_div_cls_code": "0",
                "fid_rsfl_rate1": _num(q.min_rate), "fid_rsfl_rate2": _num(q.max_rate)}, "output"
        return tr.KR_VOLUME_POWER, {**common, "fid_cond_scr_div_code": "20168",
                                    "fid_div_cls_code": _STOCK_TYPE[q.stock_type], "fid_trgt_cls_code": "0"}, "output"
    base = {"EXCD": q.exchange, "MINX": str(US_MINUTE_INDEX[q.minutes]), "VOL_RANG": str(q.us_volume_step),
            "KEYB": "", "AUTH": ""}
    if q.kind == "us_volume_surge":
        return tr.US_VOLUME_SURGE, base, "output2"
    return tr.US_PRICE_FLUCT, {**base, "GUBN": "1" if q.sort == "up" else "0"}, "output2"



def _d(raw: Any) -> Decimal:
    try:
        return Decimal(str(raw).strip().lstrip("+") or "0")
    except Exception:
        return Decimal(0)


def _i(raw: Any) -> int:
    return int(_d(raw))


def _signed(raw: Any, sign: Any) -> Decimal:
    """대비 부호 1 상한, 2 상승, 3 보합, 4 하한, 5 하락. 금액이 부호 없이 올 때가 있어 부호 코드로 맞춘다."""
    v = abs(_d(raw))
    return -v if str(sign) in ("4", "5") else v


def rows(q: RankingQuery, out: list[dict]) -> list[dict]:
    result = []
    for n, r in enumerate(out, 1):
        if q.market == "KR":
            code = r.get("mksc_shrn_iscd") or r.get("stck_shrn_iscd") or ""
            row = {"rank": _i(r.get("data_rank")) or n, "symbol": f"KR:{code}", "name": r.get("hts_kor_isnm", ""),
                   "last": _d(r.get("stck_prpr")), "change": _signed(r.get("prdy_vrss"), r.get("prdy_vrss_sign")),
                   "change_rate": _d(r.get("prdy_ctrt")), "volume": _i(r.get("acml_vol"))}
            if q.kind == "kr_volume":
                row |= {"prev_volume": _i(r.get("prdy_vol")), "avg_volume": _i(r.get("avrg_vol")),
                        "volume_growth_rate": _d(r.get("vol_inrt")), "volume_turnover": _d(r.get("vol_tnrt")),
                        "value": _d(r.get("acml_tr_pbmn")), "avg_value": _d(r.get("avrg_tr_pbmn")),
                        "value_turnover": _d(r.get("tr_pbmn_tnrt")), "listed_shares": _i(r.get("lstn_stcn"))}
            elif q.kind == "kr_fluctuation":
                row |= {"open_change_rate": _d(r.get("oprc_vrss_prpr_rate")),
                        "high": _d(r.get("stck_hgpr")), "high_time": r.get("hgpr_hour", ""),
                        "low": _d(r.get("stck_lwpr")), "low_time": r.get("lwpr_hour", ""),
                        "range_rate": _d(r.get("prd_rsfl_rate")),
                        "up_days": _i(r.get("cnnt_ascn_dynu")), "down_days": _i(r.get("cnnt_down_dynu"))}
            else:
                row |= {"strength": _d(r.get("tday_rltv")), "buy_volume": _i(r.get("shnu_cnqn_smtn")),
                        "sell_volume": _i(r.get("seln_cnqn_smtn"))}
        else:
            row = {"rank": n, "symbol": f"US:{r.get('symb', '')}", "name": r.get("knam") or r.get("enam", ""),
                   "name_en": r.get("enam", ""), "exchange": r.get("excd", q.exchange),
                   "last": _d(r.get("last")), "change": _signed(r.get("diff"), r.get("sign")),
                   "change_rate": _d(r.get("rate")), "volume": _i(r.get("tvol")),
                   "bid": _d(r.get("pbid")), "ask": _d(r.get("pask"))}
            if q.kind == "us_volume_surge":
                row |= {"base_volume": _i(r.get("n_tvol")), "volume_increase": _i(r.get("n_diff")),
                        "volume_increase_rate": _d(r.get("n_rate"))}
            else:
                rate = _d(r.get("n_rate"))   # n_diff 는 부호 없이 온다 (2026-10-17 실측) → 비율 부호를 따른다
                diff = abs(_d(r.get("n_diff")))
                row |= {"base_price": _d(r.get("n_base")), "base_change": -diff if rate < 0 else diff,
                        "base_change_rate": rate}
        if row["symbol"] not in ("KR:", "US:"):
            result.append(row)
    return result
