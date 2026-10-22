"""KIS REST 클라이언트: 공통 헤더, 호출 속도 제한, 오류 처리, 연속조회."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from central.broker.kis.auth import KisAuth
from central.broker.kis.tr import REST_BASE, Tr
from central.core import appstats
from central.core.callstats import CallStats, classify

log = logging.getLogger(__name__)

RATE_LIMIT_CODE = "EGW00201"                  # 초당 거래건수 초과
TOKEN_EXPIRED_CODES = {"EGW00123", "EGW00121"}  # 기간 만료 / 유효하지 않은 토큰
MAX_RATE_RETRIES = 3


class KisApiError(Exception):
    def __init__(self, tr_id: str, http_status: int, msg_cd: str, msg: str) -> None:
        super().__init__(f"{tr_id} HTTP {http_status} [{msg_cd}] {msg}")
        self.tr_id = tr_id
        self.http_status = http_status
        self.msg_cd = msg_cd
        self.msg = msg


@dataclass
class KisResponse:
    tr_id: str
    body: dict[str, Any]
    tr_cont: str = ""
    elapsed_ms: float = 0.0

    @property
    def has_next(self) -> bool:
        return self.tr_cont in ("M", "F")


class RateLimiter:
    """요청 시작 간격을 1/rate 초 이상으로 유지한다 (계좌 단위)."""

    def __init__(self, per_sec: float) -> None:
        self.interval = 1.0 / per_sec
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._next - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next = now + self.interval


@dataclass
class _Page:
    fk_param: str
    nk_param: str
    fk_resp: str
    nk_resp: str


PAGE_100 = _Page("CTX_AREA_FK100", "CTX_AREA_NK100", "ctx_area_fk100", "ctx_area_nk100")
PAGE_200 = _Page("CTX_AREA_FK200", "CTX_AREA_NK200", "ctx_area_fk200", "ctx_area_nk200")


@dataclass
class KisRestClient:
    auth: KisAuth
    http: httpx.AsyncClient
    limiter: RateLimiter = field(init=False)
    stats: CallStats = field(init=False, default_factory=CallStats)

    def __post_init__(self) -> None:
        self.limiter = RateLimiter(self.auth.account.profile.rate_limit_per_sec)

    @property
    def env(self):
        return self.auth.account.profile.env

    @property
    def cano(self) -> str:
        return self.auth.account.secrets.account_no.get_secret_value()

    @property
    def product_code(self) -> str:
        return self.auth.account.secrets.product_code

    def account_params(self) -> dict[str, str]:
        return {"CANO": self.cano, "ACNT_PRDT_CD": self.product_code}

    async def request(
        self,
        tr: Tr,
        params: dict[str, Any] | None = None,
        tr_cont: str = "",
    ) -> KisResponse:
        tr_id = tr.id(self.env)
        url = f"{REST_BASE[self.env]}{tr.path}"
        token_retried = False
        rate_retries = 0

        while True:
            headers = {
                "content-type": "application/json; charset=utf-8",
                "authorization": f"Bearer {await self.auth.access_token()}",
                "appkey": self.auth.app_key,
                "appsecret": self.auth.app_secret,
                "tr_id": tr_id,
                "custtype": "P",
                "tr_cont": tr_cont,
            }
            queued = time.monotonic()
            await self.limiter.acquire()
            appstats.count_kis_call()
            started = time.monotonic()
            wait_ms = (started - queued) * 1000
            try:
                if tr.method == "GET":
                    res = await self.http.get(url, headers=headers, params=params or {})
                else:
                    res = await self.http.post(url, headers=headers, json=params or {})
            except Exception:
                self.stats.record(classify(tr.path, tr.method), tr_id, wait_ms,
                                  (time.monotonic() - started) * 1000, False, False)
                raise
            elapsed_ms = (time.monotonic() - started) * 1000

            try:
                body = res.json()
            except ValueError:
                body = {}
            msg_cd = str(body.get("msg_cd", ""))
            rt_cd = str(body.get("rt_cd", ""))
            self.stats.record(classify(tr.path, tr.method), tr_id, wait_ms, elapsed_ms,
                              res.status_code == 200 and rt_cd == "0", msg_cd == RATE_LIMIT_CODE)

            if msg_cd == RATE_LIMIT_CODE and rate_retries < MAX_RATE_RETRIES:
                rate_retries += 1
                log.warning("%s 호출 한도 초과, %d회차 재시도", tr_id, rate_retries)
                await asyncio.sleep(1.0 * rate_retries)
                continue
            if msg_cd in TOKEN_EXPIRED_CODES and not token_retried:
                token_retried = True
                log.warning("%s 토큰 만료 응답, 재발급 후 재시도", tr_id)
                await self.auth.invalidate()
                continue
            if res.status_code != 200 or rt_cd != "0":
                raise KisApiError(tr_id, res.status_code, msg_cd, str(body.get("msg1", res.text[:200])).strip())

            return KisResponse(tr_id, body, res.headers.get("tr_cont", ""), elapsed_ms)

    async def paginate(
        self,
        tr: Tr,
        params: dict[str, Any],
        page: _Page,
        max_pages: int = 10,
    ) -> list[KisResponse]:
        """CTX_AREA_FK/NK 방식 연속조회. 모든 페이지 응답을 순서대로 반환."""
        params = {**params, page.fk_param: "", page.nk_param: ""}
        responses: list[KisResponse] = []
        tr_cont = ""
        for _ in range(max_pages):
            resp = await self.request(tr, params, tr_cont=tr_cont)
            responses.append(resp)
            if not resp.has_next:
                break
            params[page.fk_param] = resp.body.get(page.fk_resp, "")
            params[page.nk_param] = resp.body.get(page.nk_resp, "")
            tr_cont = "N"
        else:
            log.warning("%s 연속조회 %d페이지 초과, 중단", tr.id(self.env), max_pages)
        return responses
