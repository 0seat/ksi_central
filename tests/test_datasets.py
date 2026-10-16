"""순위/수급: 조건 검사, KIS 파라미터/행 변환, API 캐시 (central/marketdata/datasets.py, central/broker/kis/datasets.py)."""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

from central.broker.kis import datasets as kis
from central.broker.kis import tr
from central.marketdata.datasets import SPECS, build_query
from central.oms.errors import OmsError
from tests.test_api import Env

TODAY = date(2026, 10, 16)

# 2026-10-17 실측 응답에서 줄인 행
KR_VOLUME_RAW = {"hts_kor_isnm": "삼성전자", "mksc_shrn_iscd": "005930", "data_rank": "1", "stck_prpr": "61000",
                 "prdy_vrss_sign": "5", "prdy_vrss": "-500", "prdy_ctrt": "-0.81", "acml_vol": "1200000",
                 "prdy_vol": "1000000", "vol_inrt": "20.00", "acml_tr_pbmn": "73200000000"}
US_FLUCT_RAW = {"symb": "WBX", "knam": "월박스", "last": "2.6900", "sign": "5", "diff": "0.0300", "rate": "-1.13",
                "tvol": "78", "n_base": "2.8199", "n_diff": "0.1299", "n_rate": "-4.61"}
INVESTOR_RAW = {"stck_bsop_date": "20261016", "stck_clpr": "217000", "prdy_vrss": "7000", "prdy_vrss_sign": "2",
                "acml_vol": "18908560", "frgn_ntby_qty": "906438", "frgn_ntby_tr_pbmn": "198944",
                "pe_fund_ntby_vol": "80179", "pe_fund_ntby_tr_pbmn": "17661"}


def q(group: str, kind: str, **raw: str):
    return build_query(group, kind, raw, TODAY)


def test_build_query_validates() -> None:
    a = q("rankings", "kr_volume", universe="kosdaq", exclude="etf, etn,spac")
    assert a.get("sort") == "volume" and a.get("exclude") == ("etf", "etn", "spac") and a.market == "KR"
    assert q("flows", "investor_daily", symbol="kr:005930").get("date") == TODAY      # date 기본은 오늘
    assert q("flows", "short_sale_daily", symbol="KR:005930", start="20260901").get("start") == date(2026, 9, 1)
    assert q("rankings", "us_updown", days="5").get("days") == 5
    for group, kind, raw in [("rankings", "nope", {}), ("rankings", "kr_volume", {"sort": "up"}),
                             ("rankings", "kr_volume_power", {"sort": "volume"}),
                             ("rankings", "kr_volume", {"exclude": "bogus"}),
                             ("rankings", "kr_volume", {"min_rate": "1"}), ("rankings", "us_volume_surge", {"minutes": "7"}),
                             ("rankings", "us_volume_surge", {"exchange": "HKS"}),
                             ("rankings", "kr_fluctuation", {"stock_type": "common"}),
                             ("rankings", "kr_volume", {"min_volume": "-1"}),
                             ("flows", "investor_daily", {}), ("flows", "investor_daily", {"symbol": "US:AAPL"}),
                             ("flows", "short_sale_daily", {"symbol": "KR:005930", "start": "2026-13-01"})]:
        with pytest.raises(OmsError) as e:
            q(group, kind, **raw)
        assert e.value.code == "INVALID_REQUEST", (kind, raw)


def test_every_spec_builds_a_request() -> None:
    """모든 종류가 기본 조건으로 KIS 요청을 만들 수 있다 (선택지 순서 = KIS 코드)."""
    for (group, kind), spec in SPECS.items():
        raw = {"symbol": "KR:005930"} if any(o.name == "symbol" for o in spec.opts) else {}
        trx, params, key = kis.request(q(group, kind, **raw))
        assert trx.path.startswith("/uapi/") and key.startswith("output"), kind
        assert all(isinstance(v, str) for v in params.values()), kind


def test_kis_params() -> None:
    trx, p, key = kis.request(q("rankings", "kr_volume", sort="volume_growth", universe="kospi",
                                exclude="etf,etn,spac", min_volume="100000", min_price="1000"))
    assert trx is tr.KR_VOLUME_RANK and key == "output"
    assert p["FID_BLNG_CLS_CODE"] == "1" and p["FID_INPUT_ISCD"] == "0001"
    assert p["FID_TRGT_EXLS_CLS_CODE"] == "0000001101" and p["FID_VOL_CNT"] == "100000"
    assert p["FID_INPUT_PRICE_1"] == "1000" and p["FID_INPUT_PRICE_2"] == ""

    _, p, _ = kis.request(q("rankings", "kr_fluctuation", sort="down", min_rate="-10"))
    assert p["FID_RANK_SORT_CLS_CODE"] == "1" and p["FID_PRC_CLS_CODE"] == "1" and p["FID_RSFL_RATE1"] == "-10"
    _, p, key = kis.request(q("rankings", "kr_credit", sort="stock_loan_ratio_up", days="5"))
    assert p["FID_RANK_SORT_CLS_CODE"] == "8" and p["FID_OPTION"] == "5" and key == "output2"
    _, p, _ = kis.request(q("rankings", "kr_investor_net", sort="sell", investor="institution", by="qty"))
    assert (p["FID_RANK_SORT_CLS_CODE"], p["FID_ETC_CLS_CODE"], p["FID_DIV_CLS_CODE"]) == ("1", "2", "0")
    _, p, _ = kis.request(q("rankings", "kr_short_sale", period="2w"))
    assert (p["FID_PERIOD_DIV_CODE"], p["FID_INPUT_CNT_1"]) == ("D", "9")

    _, p, key = kis.request(q("rankings", "us_price_fluct", sort="down", minutes="10", min_volume="50000",
                              exchange="NYS"))
    assert p == {"EXCD": "NYS", "MINX": "4", "VOL_RANG": "3", "KEYB": "", "AUTH": "", "GUBN": "0"}
    _, p, _ = kis.request(q("rankings", "us_new_high_low", mode="touch"))
    assert p["NDAY"] == "0" and p["GUBN"] == "1" and p["GUBN2"] == "0"
    _, p, _ = kis.request(q("rankings", "us_updown", days="250"))
    assert p["NDAY"] == "9"

    _, p, key = kis.request(q("flows", "investor_daily", symbol="KR:005930"))
    assert p["FID_INPUT_ISCD"] == "005930" and p["FID_INPUT_DATE_1"] == "20261016" and key == "output2"
    _, p, _ = kis.request(q("flows", "market_investor_daily", market="kosdaq"))
    assert (p["FID_INPUT_ISCD"], p["FID_INPUT_ISCD_1"]) == ("1001", "KSQ")


def test_kis_rows() -> None:
    [r] = kis.rows(q("rankings", "kr_volume"), [KR_VOLUME_RAW])
    assert r["symbol"] == "KR:005930" and r["change"] == D("-500") and r["volume_growth_rate"] == D("20.00")
    assert r["prev_volume"] == 1000000 and isinstance(r["prev_volume"], int) and r["value"] == D("73200000000")

    [r] = kis.rows(q("rankings", "us_price_fluct", sort="down"), [US_FLUCT_RAW])
    assert r["change"] == D("-0.0300") and r["base_change"] == D("-0.1299") and r["base_change_rate"] == D("-4.61")

    [r] = kis.rows(q("flows", "investor_daily", symbol="KR:005930"), [INVESTOR_RAW])
    assert r["date"] == "2026-10-16" and r["close"] == D("217000")
    assert r["investors"]["foreign"] == {"net_qty": 906438, "net_value": D("198944")}
    assert r["investors"]["private_fund"]["net_qty"] == 80179     # 사모펀드는 _ntby_vol 로 온다

    [m] = kis.rows(q("flows", "members", symbol="KR:005930"), [{
        "seln_mbcr_name1": "미래에셋증권", "seln_mbcr_no1": "00005", "total_seln_qty1": "1956990",
        "seln_mbcr_rlim1": "10.35", "shnu_mbcr_name1": "JP모간", "shnu_mbcr_glob_yn_1": "Y", "glob_ntby_qty": "679956"}])
    assert m["sell"][0]["qty"] == 1956990 and m["buy"][0]["foreign"] is True
    assert m["foreign_total"]["net_qty"] == 679956
    assert kis.rows(q("rankings", "kr_hts_views"), [{"mrkt_div_cls_code": "J", "mksc_shrn_iscd": "005930"}]) == \
        [{"rank": 1, "symbol": "KR:005930"}]


@pytest.fixture
async def env(home: Path):
    e = Env(home)
    rows = [{"rank": 1, "symbol": "KR:005930", "volume": 1200000}, {"rank": 2, "symbol": "KR:000660", "volume": 5000}]
    for b in (e.sim, e.paper):
        b.datasets["kr_volume"] = rows
        b.datasets["investor_daily"] = [{"date": "2026-10-16", "investors": {}}]
    yield e
    await e.http.aclose()
    await e.app.http.aclose()


def calls(env: Env) -> int:
    return len(env.sim.dataset_calls) + len(env.paper.dataset_calls)


async def test_api_cache_and_filter(env: Env) -> None:
    _, key = await env.new_sub("A", ["KR"])
    async with env.sdk(key) as cs:
        a = await cs.rankings("kr_volume", exclude=["etf", "spac"])
        assert a["market"] == "KR" and not a["cached"] and len(a["rows"]) == 2
        assert a["query"]["exclude"] == ["etf", "spac"] and a["units"] == {"value": "KRW"}
        b = await cs.rankings("kr_volume", exclude="etf,spac")
        assert b["cached"] and calls(env) == 1
        c = await cs.rankings("kr_volume", min_volume=10000, limit=5)
        assert [r["symbol"] for r in c["rows"]] == ["KR:005930"] and calls(env) == 2

        kinds = await cs.rankings()
        assert {k["kind"] for k in kinds} >= {"kr_volume", "us_volume_surge", "kr_investor_net"}
        assert any(k["kind"] == "investor_daily" for k in await cs.flows())
        f = await cs.flows("investor_daily", symbol="KR:005930", date=date(2026, 10, 16))
        assert f["query"] == {"symbol": "KR:005930", "date": "2026-10-16"} and len(f["rows"]) == 1

        dq = env.app.datasets.query("rankings", "kr_volume", {"sort": "value"})   # 동시 요청은 한 번만
        got = await asyncio.gather(*(env.app.datasets.get(dq) for _ in range(3)))  # (메모리 DB 는 동시 인증 불가)
        assert [g[2] for g in got].count(False) == 1

        res = await env.http.get("/market/rankings/kr_volume_power", headers={"X-API-Key": key})
        assert res.json()["error"]["code"] == "BROKER_UNAVAILABLE"
        res = await env.http.get("/market/rankings/kr_volume?sort=up", headers={"X-API-Key": key})
        assert res.status_code == 400 and res.json()["error"]["code"] == "INVALID_REQUEST"
        res = await env.http.get("/market/flows/investor_daily", headers={"X-API-Key": key})
        assert res.status_code == 400
    assert (await env.http.get("/market/rankings/kr_volume")).status_code == 401
