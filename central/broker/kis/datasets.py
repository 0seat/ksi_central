"""KIS 순위/수급 API 의 요청 파라미터와 응답 변환 (central/marketdata/datasets.py 의 DatasetQuery).

출처: examples_llm/domestic_stock/*, overseas_stock/* 의 공식 예제. 2026-10-17 실측으로 고친 것:
- 등락률 순위 fid_rank_sort_cls_code 는 한 자리 (예제의 "0000" 은 OPSQ2002). 0 상승, 1 하락, 2 시가 대비 상승,
  3 시가 대비 하락, 4 고저 폭. fid_prc_cls_code 0 은 저가 대비, 1 은 전일 종가 대비 정렬이라 1 을 쓴다.
- 대상 제외 10자리 코드는 거래량/등락률/체결강도 순위가 받는다.
- 외국계 매매 가집계의 업종 코드는 다른 순위와 같다 (예제 설명의 1001=코스피는 틀림, 1001 은 코스닥).
- 미국 신고/신저가는 예제에 없는 NDAY, 미국 시가총액은 CURR_GB 가 필수.
- 미국 순위의 대비(diff)와 기준 대비(n_diff)는 부호 없이 온다 → 부호 코드나 비율 부호를 따른다.
- 금액 단위: 투자자별/대차/시장 프로그램 대금은 백만원, 종목 프로그램 대금과 거래대금은 원, 증시자금은 억원.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Callable

from central.broker.kis import tr
from central.broker.kis.tr import Tr
from central.marketdata.datasets import EXCLUDES, US_DAYS, US_MINUTES, DatasetQuery

_DS, _OS = "/uapi/domestic-stock/v1", "/uapi/overseas-stock/v1"
_Q, _K = f"{_DS}/quotations", f"{_DS}/ranking"


def _t(path: str, tr_id: str) -> Tr:
    return Tr(path, "GET", tr_id, tr_id)


UNIVERSE = {"all": "0000", "kospi": "0001", "kosdaq": "1001", "kospi200": "2001"}
STOCK_TYPE = {"all": "0", "common": "1", "preferred": "2"}
KR_MARKET = {"kospi": ("K", "KSP", "0001", "1"), "kosdaq": ("Q", "KSQ", "1001", "4")}
INVESTORS = (("foreign", "frgn"), ("individual", "prsn"), ("institution", "orgn"), ("securities", "scrt"),
             ("investment_trust", "ivtr"), ("private_fund", "pe_fund"), ("bank", "bank"), ("insurance", "insu"),
             ("merchant_bank", "mrbn"), ("pension_fund", "fund"), ("other_corp", "etc_corp"),
             ("other_org", "etc_orgt"))


# ───────────── 값 변환 ─────────────
def _d(raw: Any) -> Decimal:
    try:
        return Decimal(str(raw).strip().lstrip("+") or "0")
    except Exception:
        return Decimal(0)


def _i(raw: Any) -> int:
    return int(_d(raw))


def _num(v: Decimal | int | None, empty: str = "") -> str:
    return empty if v is None else format(v, "f") if isinstance(v, Decimal) else str(v)


def _ymd(v: date | None) -> str:
    return v.strftime("%Y%m%d") if v else ""


def _date(raw: Any) -> str | None:
    s = str(raw or "").strip()
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else None


def _time(raw: Any) -> str | None:
    s = str(raw or "").strip()
    return f"{s[:2]}:{s[2:4]}:{s[4:6]}" if len(s) == 6 and s.isdigit() else None


def _signed(raw: Any, sign: Any) -> Decimal:
    """대비 부호 1 상한, 2 상승, 3 보합, 4 하한, 5 하락."""
    v = abs(_d(raw))
    return -v if str(sign) in ("4", "5") else v


def _like(raw: Any, rate: Decimal) -> Decimal:
    v = abs(_d(raw))
    return -v if rate < 0 else v


def _pick(r: dict, fields: dict[str, str], conv: Callable = _d) -> dict:
    return {ours: conv(r.get(kis)) for ours, kis in fields.items()}


def _investors(r: dict) -> dict:
    out = {}
    for name, p in INVESTORS:
        net = r.get(f"{p}_ntby_qty", r.get(f"{p}_ntby_vol"))
        if net is None:
            continue
        d = {"net_qty": _i(net)}
        for ours, k in (("net_value", f"{p}_ntby_tr_pbmn"), ("buy_qty", f"{p}_shnu_vol"),
                        ("sell_qty", f"{p}_seln_vol"), ("buy_value", f"{p}_shnu_tr_pbmn"),
                        ("sell_value", f"{p}_seln_tr_pbmn")):
            if k in r:
                d[ours] = _i(r[k])
        out[name] = d
    return out


def _kr(r: dict, n: int, volume_key: str = "acml_vol") -> dict:
    code = r.get("mksc_shrn_iscd") or r.get("stck_shrn_iscd") or ""
    return {"rank": _i(r.get("data_rank")) or n, "symbol": f"KR:{code}", "name": r.get("hts_kor_isnm", ""),
            "last": _d(r.get("stck_prpr")), "change": _signed(r.get("prdy_vrss"), r.get("prdy_vrss_sign")),
            "change_rate": _d(r.get("prdy_ctrt")), "volume": _i(r.get(volume_key))}


def _us(r: dict, n: int, q: DatasetQuery) -> dict:
    return {"rank": _i(r.get("rank")) or n, "symbol": f"US:{r.get('symb', '')}",
            "name": r.get("knam") or r.get("name") or r.get("enam", ""),
            "name_en": r.get("enam") or r.get("ename", ""), "exchange": r.get("excd", q.get("exchange")),
            "last": _d(r.get("last")), "change": _signed(r.get("diff"), r.get("sign")),
            "change_rate": _d(r.get("rate")), "volume": _i(r.get("tvol")),
            "bid": _d(r.get("pbid")), "ask": _d(r.get("pask"))}


def _bar(r: dict) -> dict:
    """일별 행 공통: 날짜, 종가, 대비, 거래량."""
    return {"date": _date(r.get("stck_bsop_date") or r.get("deal_date") or r.get("bsop_date")),
            "close": _d(r.get("stck_clpr") or r.get("stck_prpr")),
            "change": _signed(r.get("prdy_vrss"), r.get("prdy_vrss_sign")),
            "change_rate": _d(r.get("prdy_ctrt")), "volume": _i(r.get("acml_vol"))}


# ───────────── 순위: 요청 ─────────────
def _kr_filters(q: DatasetQuery, *, prices=("FID_INPUT_PRICE_1", "FID_INPUT_PRICE_2"), vol="FID_VOL_CNT") -> dict:
    p = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": UNIVERSE[q.get("universe", "all")]}
    if prices:
        p[prices[0]] = _num(q.get("min_price"))
        p[prices[1]] = _num(q.get("max_price"))
    if vol:
        p[vol] = _num(q.get("min_volume"))
    return p


def _excl(q: DatasetQuery) -> str:
    return "".join("1" if x in q.get("exclude", ()) else "0" for x in EXCLUDES)


def _idx(q: DatasetQuery, name: str = "sort") -> str:
    """선택지 순서가 곧 KIS 코드인 조건 (spec 의 choices 순서를 KIS 코드 순서에 맞춰 두었다)."""
    opt = next(o for o in q.spec.opts if o.name == name)
    return str(opt.choices.index(q.get(name)))


def _us_req(q: DatasetQuery, **extra: str) -> dict:
    p = {"EXCD": q.get("exchange"), "VOL_RANG": str(q.us_volume_step), "KEYB": "", "AUTH": ""}
    if q.get("minutes") is not None:
        p["MINX"] = str(US_MINUTES.index(q.get("minutes")))
    if q.get("days") is not None:
        p["NDAY"] = str(US_DAYS.index(q.get("days")))
    return p | extra


_SHORT_PERIOD = {"1d": ("D", "0"), "2d": ("D", "1"), "3d": ("D", "2"), "4d": ("D", "3"), "1w": ("D", "4"),
                 "2w": ("D", "9"), "3w": ("D", "14"), "1m": ("M", "1"), "2m": ("M", "2"), "3m": ("M", "3")}
_NEAR = {"limit": "0", "8": "6", "10": "5", "15": "1", "20": "2", "25": "3"}


def _req_rankings(q: DatasetQuery) -> tuple[Tr, dict, str]:
    k = q.kind
    if k == "kr_volume":
        return tr.KR_VOLUME_RANK, _kr_filters(q) | {
            "FID_COND_SCR_DIV_CODE": "20171", "FID_DIV_CLS_CODE": STOCK_TYPE[q.get("stock_type")],
            "FID_BLNG_CLS_CODE": _idx(q), "FID_TRGT_CLS_CODE": "111111111", "FID_TRGT_EXLS_CLS_CODE": _excl(q),
            "FID_INPUT_DATE_1": ""}, "output"
    if k == "kr_fluctuation":
        return tr.KR_FLUCTUATION, _kr_filters(q) | {
            "FID_COND_SCR_DIV_CODE": "20170", "FID_RANK_SORT_CLS_CODE": _idx(q), "FID_INPUT_CNT_1": "0",
            "FID_PRC_CLS_CODE": "1", "FID_TRGT_CLS_CODE": "0", "FID_TRGT_EXLS_CLS_CODE": _excl(q),
            "FID_DIV_CLS_CODE": "0", "FID_RSFL_RATE1": _num(q.get("min_rate")),
            "FID_RSFL_RATE2": _num(q.get("max_rate"))}, "output"
    if k == "kr_volume_power":
        return tr.KR_VOLUME_POWER, _kr_filters(q) | {
            "FID_COND_SCR_DIV_CODE": "20168", "FID_DIV_CLS_CODE": STOCK_TYPE[q.get("stock_type")],
            "FID_TRGT_CLS_CODE": "0", "FID_TRGT_EXLS_CLS_CODE": _excl(q)}, "output"
    if k == "kr_near_high_low":
        return _t(f"{_K}/near-new-highlow", "FHPST01870000"), _kr_filters(
            q, prices=("FID_APLY_RANG_PRC_1", "FID_APLY_RANG_PRC_2"), vol=None) | {
            "FID_COND_SCR_DIV_CODE": "20187", "FID_PRC_CLS_CODE": _idx(q), "FID_DIV_CLS_CODE": "0",
            "FID_INPUT_CNT_1": "0", "FID_INPUT_CNT_2": _num(q.get("max_gap"), "100"),
            "FID_APLY_RANG_VOL": "100" if (q.get("min_volume") or 0) >= 100 else "0",
            "FID_TRGT_CLS_CODE": "0", "FID_TRGT_EXLS_CLS_CODE": "0"}, "output"
    if k == "kr_disparity":
        return _t(f"{_K}/disparity", "FHPST01780000"), _kr_filters(q) | {
            "FID_COND_SCR_DIV_CODE": "20178", "FID_DIV_CLS_CODE": "0", "FID_RANK_SORT_CLS_CODE": _idx(q),
            "FID_HOUR_CLS_CODE": str(q.get("period")), "FID_TRGT_CLS_CODE": "0", "FID_TRGT_EXLS_CLS_CODE": "0"}, "output"
    if k == "kr_expected":
        return _t(f"{_K}/exp-trans-updown", "FHPST01820000"), _kr_filters(
            q, prices=None) | {
            "FID_COND_SCR_DIV_CODE": "20182", "FID_RANK_SORT_CLS_CODE": _idx(q),
            "FID_DIV_CLS_CODE": STOCK_TYPE[q.get("stock_type")], "FID_APLY_RANG_PRC_1": _num(q.get("min_price")),
            "FID_PBMN": "", "FID_BLNG_CLS_CODE": "0",
            "FID_MKOP_CLS_CODE": "0" if q.get("session") == "open" else "1"}, "output"
    if k == "kr_order_imbalance":
        return _t(f"{_K}/quote-balance", "FHPST01720000"), _kr_filters(q) | {
            "FID_COND_SCR_DIV_CODE": "20172", "FID_RANK_SORT_CLS_CODE": _idx(q), "FID_DIV_CLS_CODE": "0",
            "FID_TRGT_CLS_CODE": "0", "FID_TRGT_EXLS_CLS_CODE": "0"}, "output"
    if k == "kr_short_sale":
        div, cnt = _SHORT_PERIOD[q.get("period")]
        return _t(f"{_K}/short-sale", "FHPST04820000"), _kr_filters(
            q, prices=("FID_APLY_RANG_PRC_1", "FID_APLY_RANG_PRC_2"), vol="FID_APLY_RANG_VOL") | {
            "FID_COND_SCR_DIV_CODE": "20482", "FID_PERIOD_DIV_CODE": div, "FID_INPUT_CNT_1": cnt,
            "FID_TRGT_EXLS_CLS_CODE": "", "FID_TRGT_CLS_CODE": ""}, "output"
    if k == "kr_credit":
        return _t(f"{_K}/credit-balance", "FHKST17010000"), {
            "FID_COND_MRKT_DIV_CODE": "J", "FID_COND_SCR_DIV_CODE": "11701",
            "FID_INPUT_ISCD": UNIVERSE[q.get("universe")], "FID_OPTION": str(q.get("days")),
            "FID_RANK_SORT_CLS_CODE": _idx(q)}, "output2"
    if k == "kr_interest":
        return _t(f"{_K}/top-interest-stock", "FHPST01800000"), _kr_filters(q) | {
            "FID_INPUT_ISCD_2": "000000", "FID_COND_SCR_DIV_CODE": "20180", "FID_TRGT_CLS_CODE": "0",
            "FID_TRGT_EXLS_CLS_CODE": "0", "FID_DIV_CLS_CODE": "0", "FID_INPUT_CNT_1": "1"}, "output"
    if k == "kr_hts_views":
        return _t(f"{_K}/hts-top-view", "HHMCM000100C0"), {}, "output1"
    if k == "kr_market_cap":
        return tr.KR_MARKET_CAP, _kr_filters(q) | {
            "FID_COND_SCR_DIV_CODE": "20174", "FID_DIV_CLS_CODE": STOCK_TYPE[q.get("stock_type")],
            "FID_TRGT_CLS_CODE": "0", "FID_TRGT_EXLS_CLS_CODE": "0"}, "output"
    if k == "kr_limit":
        return _t(f"{_Q}/capture-uplowprice", "FHKST130000C0"), _kr_filters(q) | {
            "FID_COND_SCR_DIV_CODE": "11300", "FID_PRC_CLS_CODE": _idx(q), "FID_DIV_CLS_CODE": _NEAR[q.get("near")],
            "FID_TRGT_CLS_CODE": "", "FID_TRGT_EXLS_CLS_CODE": ""}, "output"
    if k == "kr_investor_net":
        return _t(f"{_Q}/foreign-institution-total", "FHPTJ04400000"), {
            "FID_COND_MRKT_DIV_CODE": "V", "FID_COND_SCR_DIV_CODE": "16449",
            "FID_INPUT_ISCD": UNIVERSE[q.get("universe")], "FID_DIV_CLS_CODE": "0" if q.get("by") == "qty" else "1",
            "FID_RANK_SORT_CLS_CODE": _idx(q), "FID_ETC_CLS_CODE": _idx(q, "investor")}, "output"
    if k == "kr_foreign_broker":
        return _t(f"{_Q}/frgnmem-trade-estimate", "FHKST644100C0"), {
            "FID_COND_MRKT_DIV_CODE": "J", "FID_COND_SCR_DIV_CODE": "16441",
            "FID_INPUT_ISCD": UNIVERSE[q.get("universe")], "FID_RANK_SORT_CLS_CODE": _idx(q, "by"),
            "FID_RANK_SORT_CLS_CODE_2": _idx(q)}, "output"
    up = "1" if q.get("sort") in ("up", "high") else "0"
    prices = {"PRC1": _num(q.get("min_price")), "PRC2": _num(q.get("max_price"))}
    return {
        "us_volume_surge": lambda: (tr.US_VOLUME_SURGE, _us_req(q)),
        "us_price_fluct": lambda: (tr.US_PRICE_FLUCT, _us_req(q, GUBN=up)),
        "us_updown": lambda: (_t(f"{_OS}/ranking/updown-rate", "HHDFS76290000"), _us_req(q, GUBN=up)),
        "us_new_high_low": lambda: (_t(f"{_OS}/ranking/new-highlow", "HHDFS76300000"), _us_req(
            q, GUBN=up, GUBN2="1" if q.get("mode") == "hold" else "0", NDAY="0")),
        "us_volume": lambda: (_t(f"{_OS}/ranking/trade-vol", "HHDFS76310010"), _us_req(q, **prices)),
        "us_value": lambda: (_t(f"{_OS}/ranking/trade-pbmn", "HHDFS76320010"), _us_req(q, **prices)),
        "us_volume_growth": lambda: (_t(f"{_OS}/ranking/trade-growth", "HHDFS76330000"), _us_req(q)),
        "us_turnover": lambda: (_t(f"{_OS}/ranking/trade-turnover", "HHDFS76340000"), _us_req(q)),
        "us_volume_power": lambda: (_t(f"{_OS}/ranking/volume-power", "HHDFS76280000"), _us_req(q)),
        "us_market_cap": lambda: (tr.US_MARKET_CAP, _us_req(q, CURR_GB="0")),
    }[k]() + ("output2",)


# ───────────── 순위: 행 변환 ─────────────
_KR_EXTRA: dict[str, dict[str, str]] = {
    "kr_volume": {"prev_volume": "prdy_vol", "avg_volume": "avrg_vol", "volume_growth_rate": "vol_inrt",
                  "volume_turnover": "vol_tnrt", "value": "acml_tr_pbmn", "avg_value": "avrg_tr_pbmn",
                  "value_turnover": "tr_pbmn_tnrt", "listed_shares": "lstn_stcn"},
    "kr_fluctuation": {"open_change_rate": "oprc_vrss_prpr_rate", "high": "stck_hgpr", "low": "stck_lwpr",
                       "range_rate": "prd_rsfl_rate", "up_days": "cnnt_ascn_dynu", "down_days": "cnnt_down_dynu"},
    "kr_volume_power": {"strength": "tday_rltv", "buy_volume": "shnu_cnqn_smtn", "sell_volume": "seln_cnqn_smtn"},
    "kr_near_high_low": {"new_high": "new_hgpr", "high_gap_rate": "hprc_near_rate", "new_low": "new_lwpr",
                         "low_gap_rate": "lwpr_near_rate", "base_price": "stck_sdpr", "bid": "bidp", "ask": "askp",
                         "bid_size": "bidp_rsqn1", "ask_size": "askp_rsqn1"},
    "kr_disparity": {f"disparity_{n}": f"d{n}_dsrt" for n in (5, 10, 20, 60, 120)},
    "kr_expected": {"base_price": "stck_sdpr", "expected_value": "antc_tr_pbmn", "bid": "bidp", "ask": "askp",
                    "bid_size": "shnu_rsqn", "ask_size": "seln_rsqn", "total_bid_size": "total_bidp_rsqn",
                    "total_ask_size": "total_askp_rsqn"},
    "kr_order_imbalance": {"total_ask_size": "total_askp_rsqn", "total_bid_size": "total_bidp_rsqn",
                           "net_bid_size": "total_ntsl_bidp_rsqn", "bid_ratio": "shnu_rsqn_rate",
                           "ask_ratio": "seln_rsqn_rate"},
    "kr_short_sale": {"value": "acml_tr_pbmn", "short_qty": "ssts_cntg_qty", "short_qty_ratio": "ssts_vol_rlim",
                      "short_value": "ssts_tr_pbmn", "short_value_ratio": "ssts_tr_pbmn_rlim", "avg_price": "avrg_prc"},
    "kr_credit": {"margin_shares": "whol_loan_rmnd_stcn", "margin_amount": "whol_loan_rmnd_amt",
                  "margin_ratio": "whol_loan_rmnd_rate", "margin_change_rate": "nday_vrss_loan_rmnd_inrt",
                  "stock_loan_shares": "whol_stln_rmnd_stcn", "stock_loan_amount": "whol_stln_rmnd_amt",
                  "stock_loan_ratio": "whol_stln_rmnd_rate", "stock_loan_change_rate": "nday_vrss_stln_rmnd_inrt"},
    "kr_interest": {"value": "acml_tr_pbmn", "bid": "bidp", "ask": "askp", "interest_count": "inter_issu_reg_csnu"},
    "kr_market_cap": {"listed_shares": "lstn_stcn", "market_cap": "stck_avls", "market_weight": "mrkt_whol_avls_rlim"},
    "kr_limit": {"upper_limit": "stck_mxpr", "lower_limit": "stck_llam", "total_ask_size": "total_askp_rsqn",
                 "total_bid_size": "total_bidp_rsqn", "ask_size": "askp_rsqn1", "bid_size": "bidp_rsqn1",
                 "prev_volume": "prdy_vol", "buy_volume": "shnu_cnqn", "sell_volume": "seln_cnqn",
                 "volume_ratio": "prdy_vrss_vol_rate"},
    "kr_foreign_broker": {"net_qty": "glob_ntsl_qty", "sell_qty": "glob_total_seln_qty",
                          "buy_qty": "glob_total_shnu_qty"},
}
_US_EXTRA: dict[str, dict[str, str]] = {
    "us_volume_surge": {"base_volume": "n_tvol", "volume_increase": "n_diff", "volume_increase_rate": "n_rate"},
    "us_volume": {"value": "tamt", "avg_volume": "a_tvol"},
    "us_value": {"value": "tamt", "avg_value": "a_tamt"},
    "us_volume_growth": {"avg_volume": "n_tvol", "volume_growth_rate": "n_rate"},
    "us_turnover": {"listed_shares": "shar", "turnover": "tover"},
    "us_volume_power": {"strength": "tpow", "strength_now": "powx"},
    "us_market_cap": {"listed_shares": "shar", "market_cap": "tomv", "market_weight": "grav"},
}
_US_BASE_PRICE = ("us_price_fluct", "us_updown", "us_new_high_low")


def _rows_rankings(q: DatasetQuery, out: list[dict]) -> list[dict]:
    rows = []
    for n, r in enumerate(out, 1):
        if q.kind == "kr_hts_views":
            row = {"rank": n, "symbol": f"KR:{r.get('mksc_shrn_iscd', '')}"}
        elif q.market == "KR":
            row = _kr(r, n, "cntg_vol" if q.kind == "kr_expected" else "acml_vol")
            row |= _pick(r, _KR_EXTRA.get(q.kind, {}))
            if q.kind == "kr_fluctuation":
                row |= {"high_time": _time(r.get("hgpr_hour")), "low_time": _time(r.get("lwpr_hour"))}
            elif q.kind == "kr_short_sale":
                row |= {"from_date": _date(r.get("stnd_date1")), "to_date": _date(r.get("stnd_date2"))}
            elif q.kind == "kr_investor_net":
                row |= {"net_qty": _i(r.get("ntby_qty")), "investors": _investors(r)}
        else:
            row = _us(r, n, q) | _pick(r, _US_EXTRA.get(q.kind, {}))
            if q.kind in _US_BASE_PRICE:
                rate = _d(r.get("n_rate"))
                row |= {"base_price": _d(r.get("n_base")), "base_change": _like(r.get("n_diff"), rate),
                        "base_change_rate": rate}
        if row["symbol"] not in ("KR:", "US:"):
            rows.append(row)
    return rows


# ───────────── 수급 ─────────────
def _req_flows(q: DatasetQuery) -> tuple[Tr, dict, str]:
    k, code = q.kind, q.code
    start, end, day = q.get("start"), q.get("end"), q.get("date")
    if k == "investor_daily":
        return _t(f"{_Q}/investor-trade-by-stock-daily", "FHPTJ04160001"), {
            "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code, "FID_INPUT_DATE_1": _ymd(day),
            "FID_ORG_ADJ_PRC": "", "FID_ETC_CLS_CODE": ""}, "output2"
    if k == "investor_estimate":
        return _t(f"{_Q}/investor-trend-estimate", "HHPTJ04160200"), {"MKSC_SHRN_ISCD": code}, "output2"
    if k == "program":
        return _t(f"{_Q}/program-trade-by-stock", "FHPPG04650101"), {
            "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}, "output"
    if k == "program_daily":
        return _t(f"{_Q}/program-trade-by-stock-daily", "FHPPG04650201"), {
            "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code, "FID_INPUT_DATE_1": _ymd(day)}, "output"
    if k == "foreign_broker":
        return _t(f"{_Q}/frgnmem-pchs-trend", "FHKST644400C0"), {
            "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code, "FID_INPUT_ISCD_2": "99999"}, "output"
    if k == "members":
        return _t(f"{_Q}/inquire-member", "FHKST01010600"), {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}, "output"
    if k == "buy_sell_daily":
        return _t(f"{_Q}/inquire-daily-trade-volume", "FHKST03010800"), {
            "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code, "FID_PERIOD_DIV_CODE": "D",
            "FID_INPUT_DATE_1": _ymd(start), "FID_INPUT_DATE_2": _ymd(end)}, "output2"
    if k == "short_sale_daily":
        return _t(f"{_Q}/daily-short-sale", "FHPST04830000"), {
            "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code, "FID_INPUT_DATE_1": _ymd(start),
            "FID_INPUT_DATE_2": _ymd(end)}, "output2"
    if k == "credit_daily":
        return _t(f"{_Q}/daily-credit-balance", "FHPST04760000"), {
            "FID_COND_MRKT_DIV_CODE": "J", "FID_COND_SCR_DIV_CODE": "20476", "FID_INPUT_ISCD": code,
            "FID_INPUT_DATE_1": _ymd(day)}, "output"
    if k == "stock_loan_daily":
        return _t(f"{_Q}/daily-loan-trans", "HHPST074500C0"), {
            "MRKT_DIV_CLS_CODE": "3", "MKSC_SHRN_ISCD": code, "START_DATE": _ymd(start), "END_DATE": _ymd(end),
            "CTS": ""}, "output1"
    mkt, idx_code, upjong, prog = KR_MARKET[q.get("market", "kospi")]
    if k == "market_investor_daily":
        return _t(f"{_Q}/inquire-investor-daily-by-market", "FHPTJ04040000"), {
            "FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": upjong, "FID_INPUT_DATE_1": _ymd(day),
            "FID_INPUT_ISCD_1": idx_code, "FID_INPUT_DATE_2": _ymd(day), "FID_INPUT_ISCD_2": upjong}, "output"
    if k == "market_program":
        return _t(f"{_Q}/comp-program-trade-today", "FHPPG04600101"), {
            "FID_COND_MRKT_DIV_CODE": "J", "FID_MRKT_CLS_CODE": mkt, "FID_SCTN_CLS_CODE": "", "FID_INPUT_ISCD": "",
            "FID_COND_MRKT_DIV_CODE1": "", "FID_INPUT_HOUR_1": ""}, "output"
    if k == "market_program_daily":
        return _t(f"{_Q}/comp-program-trade-daily", "FHPPG04600001"), {
            "FID_COND_MRKT_DIV_CODE": "J", "FID_MRKT_CLS_CODE": mkt, "FID_INPUT_DATE_1": _ymd(start),
            "FID_INPUT_DATE_2": _ymd(end)}, "output"
    if k == "market_program_investor":
        return _t(f"{_Q}/investor-program-trade-today", "HHPPG046600C1"), {
            "EXCH_DIV_CLS_CODE": "J", "MRKT_DIV_CLS_CODE": prog}, "output1"
    return _t(f"{_Q}/mktfunds", "FHKST649100C0"), {"FID_INPUT_DATE_1": _ymd(day)}, "output"


def _flow_sides(r: dict, prefix: str, *, qty: str = "vol", value: str = "tr_pbmn") -> dict:
    return {"sell_qty": _i(r.get(f"{prefix}_seln_{qty}")), "buy_qty": _i(r.get(f"{prefix}_shnu_{qty}")),
            "net_qty": _i(r.get(f"{prefix}_ntby_qty")), "sell_value": _i(r.get(f"{prefix}_seln_{value}")),
            "buy_value": _i(r.get(f"{prefix}_shnu_{value}")), "net_value": _i(r.get(f"{prefix}_ntby_{value}"))}


def _rows_flows(q: DatasetQuery, out: list[dict]) -> list[dict]:
    k = q.kind
    if k == "members":
        r = out[0] if out else {}

        def side(s: str) -> list[dict]:
            return [{"broker_no": r.get(f"{s}_mbcr_no{i}", ""), "broker": r.get(f"{s}_mbcr_name{i}", ""),
                     "qty": _i(r.get(f"total_{s}_qty{i}")), "ratio": _d(r.get(f"{s}_mbcr_rlim{i}")),
                     "qty_change": _i(r.get(f"{s}_qty_icdc{i}")), "foreign": r.get(f"{s}_mbcr_glob_yn_{i}") == "Y"}
                    for i in range(1, 6) if r.get(f"{s}_mbcr_name{i}")]
        return [{"sell": side("seln"), "buy": side("shnu"), "foreign_total": {
            "sell_qty": _i(r.get("glob_total_seln_qty")), "buy_qty": _i(r.get("glob_total_shnu_qty")),
            "net_qty": _i(r.get("glob_ntby_qty")), "sell_ratio": _d(r.get("glob_seln_rlim")),
            "buy_ratio": _d(r.get("glob_shnu_rlim"))}}] if r else []
    rows = []
    for r in out:
        if k == "investor_daily":
            row = _bar(r) | {"value": _i(r.get("acml_tr_pbmn")), "open": _d(r.get("stck_oprc")),
                             "high": _d(r.get("stck_hgpr")), "low": _d(r.get("stck_lwpr")),
                             "investors": _investors(r)}
        elif k == "investor_estimate":
            row = {"slot": _i(r.get("bsop_hour_gb")), "foreign_net_qty": _i(r.get("frgn_fake_ntby_qty")),
                   "institution_net_qty": _i(r.get("orgn_fake_ntby_qty")), "total_net_qty": _i(r.get("sum_fake_ntby_qty"))}
        elif k in ("program", "program_daily"):
            row = (_bar(r) | {"value": _i(r.get("acml_tr_pbmn"))} if k == "program_daily" else
                   {"time": _time(r.get("bsop_hour")), "last": _d(r.get("stck_prpr")),
                    "change_rate": _d(r.get("prdy_ctrt")), "volume": _i(r.get("acml_vol"))})
            row |= {"sell_qty": _i(r.get("whol_smtn_seln_vol")), "buy_qty": _i(r.get("whol_smtn_shnu_vol")),
                    "net_qty": _i(r.get("whol_smtn_ntby_qty")), "sell_value": _i(r.get("whol_smtn_seln_tr_pbmn")),
                    "buy_value": _i(r.get("whol_smtn_shnu_tr_pbmn")), "net_value": _i(r.get("whol_smtn_ntby_tr_pbmn")),
                    "net_qty_change": _i(r.get("whol_ntby_vol_icdc"))}
        elif k == "foreign_broker":
            row = {"time": _time(r.get("bsop_hour")), "last": _d(r.get("stck_prpr")),
                   "change_rate": _d(r.get("prdy_ctrt")), "volume": _i(r.get("acml_vol")),
                   "sell_qty": _i(r.get("frgn_seln_vol")), "buy_qty": _i(r.get("frgn_shnu_vol")),
                   "net_qty": _i(r.get("glob_ntby_qty")), "net_qty_change": _i(r.get("frgn_ntby_qty_icdc"))}
        elif k == "buy_sell_daily":
            row = {"date": _date(r.get("stck_bsop_date")), "sell_qty": _i(r.get("total_seln_qty")),
                   "buy_qty": _i(r.get("total_shnu_qty"))}
        elif k == "short_sale_daily":
            row = _bar(r) | _pick(r, {"value": "acml_tr_pbmn", "short_qty": "ssts_cntg_qty",
                                      "short_qty_ratio": "ssts_vol_rlim", "short_value": "ssts_tr_pbmn",
                                      "short_value_ratio": "ssts_tr_pbmn_rlim", "cum_short_qty": "acml_ssts_cntg_qty",
                                      "cum_short_value": "acml_ssts_tr_pbmn", "avg_price": "avrg_prc"})
        elif k == "credit_daily":
            row = _bar(r) | {"settle_date": _date(r.get("stlm_date"))}
            for ours, p in (("margin", "loan"), ("stock_loan", "stln")):
                row |= _pick(r, {f"{ours}_new_shares": f"whol_{p}_new_stcn", f"{ours}_repaid_shares": f"whol_{p}_rdmp_stcn",
                                 f"{ours}_shares": f"whol_{p}_rmnd_stcn", f"{ours}_new_amount": f"whol_{p}_new_amt",
                                 f"{ours}_repaid_amount": f"whol_{p}_rdmp_amt", f"{ours}_amount": f"whol_{p}_rmnd_amt",
                                 f"{ours}_ratio": f"whol_{p}_rmnd_rate", f"{ours}_supply_rate": f"whol_{p}_gvrt"})
        elif k == "stock_loan_daily":
            row = _bar(r) | _pick(r, {"new_shares": "new_stcn", "repaid_shares": "rdmp_stcn",
                                      "balance_change": "prdy_rmnd_vrss", "balance_shares": "rmnd_stcn",
                                      "balance_amount": "rmnd_amt"}, _i)
        elif k == "market_investor_daily":
            row = {"date": _date(r.get("stck_bsop_date")), "index": _d(r.get("bstp_nmix_prpr")),
                   "change_rate": _d(r.get("bstp_nmix_prdy_ctrt")), "investors": _investors(r)}
        elif k == "market_program":
            row = {"time": _time(r.get("bsop_hour")),
                   "arbitrage": _flow_sides(r, "arbt_smtn"), "non_arbitrage": _flow_sides(r, "nabt_smtn"),
                   "net_value": _i(r.get("whol_smtn_ntby_tr_pbmn"))}
            for d in (row["arbitrage"], row["non_arbitrage"]):
                for x in ("sell_qty", "buy_qty", "net_qty"):
                    d.pop(x)
        elif k == "market_program_daily":
            row = {"date": _date(r.get("stck_bsop_date")), "arbitrage": _flow_sides(r, "arbt_smtn"),
                   "non_arbitrage": _flow_sides(r, "nabt_smtn"), "total": _flow_sides(r, "whol_smtn")}
        elif k == "market_program_investor":
            row = {"investor_code": r.get("invr_cls_code", ""), "investor": str(r.get("invr_cls_name", "")).replace(" ", "")}
            for ours, p in (("arbitrage", "arbt"), ("non_arbitrage", "nabt"), ("total", "all")):
                row[ours] = _pick(r, {"sell_qty": f"{p}_seln_qty", "buy_qty": f"{p}_shnu_qty", "net_qty": f"{p}_ntby_qty",
                                      "sell_value": f"{p}_seln_amt", "buy_value": f"{p}_shnu_amt",
                                      "net_value": f"{p}_ntby_amt"}, _i)
        else:   # market_funds
            row = {"date": _date(r.get("bsop_date")), "kospi": _d(r.get("bstp_nmix_prpr"))} | _pick(r, {
                "customer_deposit": "cust_dpmn_amt", "customer_deposit_change": "cust_dpmn_amt_prdy_vrss",
                "unsettled": "uncl_amt", "margin_loan": "crdt_loan_rmnd", "futures_deposit": "futs_tfam_amt",
                "equity_fund": "sttp_amt", "mixed_fund": "mxtp_amt", "bond_fund": "bntp_amt", "mmf": "mmf_amt",
                "secured_loan": "secu_lend_amt"}, _i)
        rows.append(row)
    return rows


_INT_SUFFIX = ("volume", "qty", "shares", "size", "count", "days", "qty_change", "balance_change")


def _normalize(v: Any, key: str = "") -> Any:
    """수량류(이름이 volume, qty, shares, size, count, days 로 끝남, rank, slot)는 정수, 나머지 숫자는 Decimal
    (JSON 에서는 문자열). 서버의 금액은 문자열 규칙을 따른다."""
    if isinstance(v, dict):
        return {k: _normalize(x, k) for k, x in v.items()}
    if isinstance(v, list):
        return [_normalize(x, key) for x in v]
    if isinstance(v, bool) or not isinstance(v, (int, Decimal)):
        return v
    if key in ("rank", "slot") or key.endswith(_INT_SUFFIX):
        return int(v)
    return Decimal(v)


def request(q: DatasetQuery) -> tuple[Tr, dict[str, str], str]:
    """(TR, 파라미터, 행이 담긴 응답 키)."""
    return _req_rankings(q) if q.group == "rankings" else _req_flows(q)


def rows(q: DatasetQuery, out: list[dict]) -> list[dict]:
    return _normalize(_rows_rankings(q, out) if q.group == "rankings" else _rows_flows(q, out))
