"""네트워크 없이 검증 가능한 KIS 클라이언트 단위 테스트."""

from __future__ import annotations

import asyncio
import json
import time
from base64 import b64encode
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from central.broker.kis import tr
from central.broker.kis.auth import KST, KisAuth, Token, TokenCache
from central.broker.kis.crypto import aes_cbc_base64_decrypt
from central.broker.kis.rest import KisApiError, KisRestClient, RateLimiter
from central.broker.kis.ws import KisWebSocket
from central.settings import load_settings


def test_tr_env_selection() -> None:
    assert tr.KR_ORDER_BUY.id("real") == "TTTC0012U"
    assert tr.KR_ORDER_BUY.id("paper") == "VTTC0012U"
    assert tr.US_ORDER_SELL.id("paper") == "VTTT1006U"
    assert tr.KR_PRICE.id("paper") == "FHKST01010100"
    with pytest.raises(ValueError):
        tr.KR_INTGR_MARGIN.id("paper")
    assert tr.us_ws_key("NASD", "AAPL") == "DNASAAPL"
    assert tr.us_ws_key("NYSE", "KO") == "DNYSKO"


def test_aes_decrypt_roundtrip() -> None:
    key, iv = "k" * 32, "i" * 16
    plain = "a^b^체결^1"
    enc = b64encode(AES.new(key.encode(), AES.MODE_CBC, iv.encode()).encrypt(pad(plain.encode(), 16))).decode()
    assert aes_cbc_base64_decrypt(key, iv, enc) == plain


async def test_rate_limiter_spacing() -> None:
    limiter = RateLimiter(per_sec=20)
    started = time.monotonic()
    await asyncio.gather(*(limiter.acquire() for _ in range(6)))
    assert time.monotonic() - started >= 5 * 0.05 * 0.9


def test_token_cache_roundtrip(tmp_path: Path) -> None:
    cache = TokenCache(tmp_path, "real")
    now = datetime.now(KST)
    t = Token("dummy-token", now + timedelta(hours=20), now)
    cache.save(t)
    assert cache.load() == t
    assert t.usable(now)
    assert not Token("x", now + timedelta(minutes=10), now).usable(now)


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_token_issue_and_reuse(home: Path) -> None:
    settings = load_settings()
    calls = {"token": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/oauth2/tokenP":
            calls["token"] += 1
            return httpx.Response(200, json={
                "access_token": "dummy-issued-token",
                "access_token_token_expired": (datetime.now(KST) + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S"),
            })
        return httpx.Response(404)

    async with _mock_client(handler) as http:
        a1 = KisAuth(settings.accounts["paper"], settings.tokens_dir, http)
        assert await a1.access_token() == "dummy-issued-token"
        assert a1.last_issue_was_new
        a2 = KisAuth(settings.accounts["paper"], settings.tokens_dir, http)
        assert await a2.access_token() == "dummy-issued-token"
        assert not a2.last_issue_was_new
    assert calls["token"] == 1


async def test_rest_headers_retry_and_error(home: Path) -> None:
    settings = load_settings()
    TokenCache(settings.tokens_dir, "paper").save(
        Token("dummy-cached", datetime.now(KST) + timedelta(hours=20), datetime.now(KST))
    )
    seen: list[httpx.Request] = []
    responses = iter([
        httpx.Response(500, json={"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수를 초과하였습니다."}),
        httpx.Response(200, json={"rt_cd": "0", "msg_cd": "MCA00000", "output": {"stck_prpr": "61000"}}, headers={"tr_cont": "D"}),
        httpx.Response(200, json={"rt_cd": "1", "msg_cd": "APBK0013", "msg1": "주문 불가"}),
    ])

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return next(responses)

    async with _mock_client(handler) as http:
        client = KisRestClient(KisAuth(settings.accounts["paper"], settings.tokens_dir, http), http)
        resp = await client.request(tr.KR_PRICE, {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": "005930"})
        assert resp.body["output"]["stck_prpr"] == "61000"
        assert not resp.has_next
        with pytest.raises(KisApiError) as exc:
            await client.request(tr.KR_ORDER_BUY, {"PDNO": "005930"})
        assert exc.value.msg_cd == "APBK0013"

    first = seen[0]
    assert first.url.host == "openapivts.koreainvestment.com"
    assert first.headers["tr_id"] == "FHKST01010100"
    assert first.headers["authorization"] == "Bearer dummy-cached"
    assert first.headers["custtype"] == "P"
    assert seen[2].method == "POST"
    assert seen[2].headers["tr_id"] == "VTTC0012U"
    assert json.loads(seen[2].content) == {"PDNO": "005930"}


async def test_ws_parse_data_and_ack(home: Path) -> None:
    settings = load_settings()
    ws = KisWebSocket(KisAuth(settings.accounts["paper"], settings.tokens_dir, httpx.AsyncClient()))

    ws._on_data("0|H0STCNT0|002|005930^090001^61000^005930^090002^61100")
    msg = ws.queue.get_nowait()
    assert msg.tr_id == "H0STCNT0" and not msg.encrypted
    assert msg.records == [["005930", "090001", "61000"], ["005930", "090002", "61100"]]

    key, iv = "k" * 32, "i" * 16
    ack = {
        "header": {"tr_id": "H0STCNI9", "tr_key": "dummyhts", "encrypt": "Y"},
        "body": {"rt_cd": "0", "msg_cd": "OPSP0000", "msg1": "SUBSCRIBE SUCCESS", "output": {"iv": iv, "key": key}},
    }
    fut = asyncio.get_running_loop().create_future()
    ws._pending[("H0STCNI9", "dummyhts")] = fut
    await ws._on_system(json.dumps(ack))
    assert fut.result().ok and fut.result().encrypted

    enc = b64encode(AES.new(key.encode(), AES.MODE_CBC, iv.encode()).encrypt(pad("a^b^c".encode(), 16))).decode()
    ws._on_data(f"1|H0STCNI9|001|{enc}")
    assert ws.queue.get_nowait().records == [["a", "b", "c"]]
