"""순위 조회: 조건 검사, KIS 파라미터/행 변환, API 캐시 (central/marketdata/rankings.py, central/broker/kis/ranking.py)."""

from __future__ import annotations

import asyncio
from decimal import Decimal as D
from pathlib import Path

import pytest

from central.broker.kis import ranking as kis
from central.broker.kis import tr
from central.marketdata.rankings import build_query
from central.oms.errors import OmsError
from tests.test_api import Env

# 2026-10-17 실측 응답에서 줄인 행
KR_VOLUME_RAW = {"hts_kor_isnm": "삼성전자", "mksc_shrn_iscd": "005930", "data_rank": "1", "stck_prpr": "61000",
                 "prdy_vrss_sign": "5", "prdy_vrss": "-500", "prdy_ctrt": "-0.81", "acml_vol": "1200000",
                 "prdy_vol": "1000000", "lstn_stcn": "5969782550", "avrg_vol": "900000", "vol_inrt": "20.00",
                 "vol_tnrt": "0.02", "avrg_tr_pbmn": "1", "tr_pbmn_tnrt": "0.01", "acml_tr_pbmn": "73200000000"}
US_SURGE_RAW = {"rsym": "DNASDBCA", "excd": "NAS", "symb": "DBCA", "knam": "보랄", "enam": "D. BORAL", "last": "10.0700",
                "sign": "5", "diff": "0.0300", "rate": "-0.30", "tvol": "52119", "pask": "10.0700", "pbid": "10.0400",
                "n_tvol": "1019", "n_diff": "51100", "n_rate": "5014.72"}


def test_build_query_validates() -> None:
    q = build_query("kr_volume", universe="kosdaq", exclude="etf, etn,spac")
    assert q.sort == "volume" and q.exclude == ("etf", "etn", "spac") and q.market == "KR"
    assert build_query("us_price_fluct").sort == "up"
    for kind, kw in [("nope", {}), ("kr_volume", {"sort": "up"}), ("kr_volume_power", {"sort": "volume"}),
                     ("kr_volume", {"exclude": "bogus"}), ("kr_volume", {"universe": "nasdaq"}),
                     ("kr_volume", {"min_rate": D(1)}), ("us_volume_surge", {"minutes": 7}),
                     ("us_volume_surge", {"exchange": "HKS"}), ("us_volume_surge", {"exclude": "etf"}),
                     ("kr_fluctuation", {"stock_type": "common"})]:
        with pytest.raises(OmsError) as e:
            build_query(kind, **kw)
        assert e.value.code == "INVALID_REQUEST", (kind, kw)


def test_kis_params() -> None:
    trx, p, key = kis.request(build_query("kr_volume", sort="volume_growth", universe="kospi",
                                          exclude="etf,etn,spac", min_volume=100000, min_price=D("1000")))
    assert trx is tr.KR_VOLUME_RANK and key == "output"
    assert p["FID_BLNG_CLS_CODE"] == "1" and p["FID_INPUT_ISCD"] == "0001"
    assert p["FID_TRGT_EXLS_CLS_CODE"] == "0000001101" and p["FID_VOL_CNT"] == "100000"
    assert p["FID_INPUT_PRICE_1"] == "1000" and p["FID_INPUT_PRICE_2"] == ""

    trx, p, _ = kis.request(build_query("kr_fluctuation", sort="down", min_rate=D("-10")))
    assert trx is tr.KR_FLUCTUATION
    assert p["fid_rank_sort_cls_code"] == "1" and p["fid_prc_cls_code"] == "1" and p["fid_rsfl_rate1"] == "-10"

    trx, p, key = kis.request(build_query("us_price_fluct", sort="down", minutes=10, min_volume=50000, exchange="nys"))
    assert trx is tr.US_PRICE_FLUCT and key == "output2"
    assert p == {"EXCD": "NYS", "MINX": "4", "VOL_RANG": "3", "KEYB": "", "AUTH": "", "GUBN": "0"}


def test_kis_rows() -> None:
    [r] = kis.rows(build_query("kr_volume"), [KR_VOLUME_RAW])
    assert r["symbol"] == "KR:005930" and r["change"] == D("-500") and r["volume_growth_rate"] == D("20.00")
    assert r["value"] == D("73200000000") and r["volume"] == 1200000
    [r] = kis.rows(build_query("us_volume_surge"), [US_SURGE_RAW])
    assert r["symbol"] == "US:DBCA" and r["change"] == D("-0.0300") and r["change_rate"] == D("-0.30")
    assert r["base_volume"] == 1019 and r["volume_increase_rate"] == D("5014.72") and r["bid"] == D("10.0400")
    [r] = kis.rows(build_query("us_price_fluct", sort="down"), [{"symb": "WBX", "n_base": "2.8199", "n_diff": "0.1299",
                                                                 "n_rate": "-4.61"}])
    assert r["base_change"] == D("-0.1299") and r["base_change_rate"] == D("-4.61")


@pytest.fixture
async def env(home: Path):
    e = Env(home)
    rows = [{"rank": 1, "symbol": "KR:005930", "volume": 1200000}, {"rank": 2, "symbol": "KR:000660", "volume": 5000}]
    for b in (e.sim, e.paper):
        b.rankings["kr_volume"] = rows
    yield e
    await e.http.aclose()
    await e.app.http.aclose()


def calls(env: Env) -> int:
    return len(env.sim.ranking_calls) + len(env.paper.ranking_calls)


async def test_api_cache_and_filter(env: Env) -> None:
    _, key = await env.new_sub("A", ["KR"])
    async with env.sdk(key) as cs:
        a = await cs.rankings("kr_volume", exclude=["etf", "spac"])
        assert a["market"] == "KR" and not a["cached"] and len(a["rows"]) == 2
        assert a["query"]["exclude"] == ["etf", "spac"]
        b = await cs.rankings("kr_volume", exclude="etf,spac")
        assert b["cached"] and calls(env) == 1
        c = await cs.rankings("kr_volume", min_volume=10000, limit=5)
        assert [r["symbol"] for r in c["rows"]] == ["KR:005930"] and calls(env) == 2

        q = build_query("kr_volume", sort="value")   # 동시에 온 같은 조건은 한 번만 (서비스 직접: 메모리 DB 는 동시 인증 불가)
        got = await asyncio.gather(*(env.app.rankings.get(q) for _ in range(3)))
        assert calls(env) == 3 and [g[2] for g in got].count(False) == 1

        res = await env.http.get("/market/rankings/kr_volume_power", headers={"X-API-Key": key})
        assert res.json()["error"]["code"] == "BROKER_UNAVAILABLE"
        res = await env.http.get("/market/rankings/kr_volume?sort=up", headers={"X-API-Key": key})
        assert res.status_code == 400 and res.json()["error"]["code"] == "INVALID_REQUEST"
    res = await env.http.get("/market/rankings/kr_volume")
    assert res.status_code == 401
