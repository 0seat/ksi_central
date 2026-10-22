"""KisBroker: Broker 인터페이스의 한국투자증권 구현 (모의/실전 공용).

- 주문/정정/취소/시세/주문내역/잔고: REST (central/broker/kis/rest.py)
- 체결통보: 계좌 공유 실시간 세션(session.py). 끊기면 지수 백오프로 재연결하고, 재연결 직후 on_reconnect
  콜백으로 주문 동기화(central/oms/sync.py)를 돌려 끊긴 동안의 체결을 보정한다.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from central.broker.base import (
    AccountSnapshot,
    Broker,
    BrokerOrderAck,
    BrokerOrderRequest,
    BrokerOrderStatus,
    BrokerReject,
    BrokerUnavailable,
    Candle,
    CashRight,
    OrderBook,
    Quote,
)
from central.broker.kis import tr
from central.broker.kis.auth import KisAuth, KisAuthError
from central.broker.kis.notice import NoticeParser
from central.broker.kis.rest import PAGE_100, PAGE_200, KisApiError, KisRestClient
from central.broker.kis.session import KisWsSession
from central.broker.kis.ws import RealtimeMessage
from central.core.instruments import Instrument
from central.core.market_hours import KST, NEW_YORK, now_kst
from central.core.money import ZERO
from central.settings import Account

log = logging.getLogger(__name__)

FX_TTL_SEC = 600


def _d(raw: Any) -> Decimal:
    try:
        return Decimal(str(raw).strip() or "0")
    except Exception:
        return ZERO


def _i(raw: Any) -> int:
    return int(_d(raw))


def _rows(body: dict, key: str) -> list[dict]:
    out = body.get(key)
    if out is None:
        return []
    return out if isinstance(out, list) else [out]


def _candles(rows: list[tuple], end: datetime) -> list[Candle]:
    out = [Candle(ts, _d(o), _d(h), _d(lo), _d(c), _i(v)) for ts, o, h, lo, c, v in rows if ts <= end and _d(c) > 0]
    out.sort(key=lambda x: x.ts)
    return out


class KisBroker(Broker):
    def __init__(
        self, account: Account, http: httpx.AsyncClient, tokens_dir: Path,
        *, clock: Callable[[], datetime] = now_kst,
    ) -> None:
        super().__init__(account.alias)
        self.account = account
        self.profile = account.profile
        self.auth = KisAuth(account, tokens_dir, http)
        self.rest = KisRestClient(self.auth, http)
        self.clock = clock
        self.on_reconnect: Callable[[], Awaitable[None]] | None = None
        # 앱키당 실시간 세션은 하나뿐이라 시세 허브(central/marketdata)도 이 세션을 같이 쓴다
        self.session = KisWsSession(self.auth)
        self.session.on_reconnect.append(self._reconnected)
        self.notice_connected = self.session.connected
        env = self.profile.env
        self._notice_markets = {
            tr.WS_KR_NOTICE[env]: "KR", tr.WS_US_NOTICE[env]: "US",
        }
        self._parsers = {"KR": NoticeParser("KR"), "US": NoticeParser("US")}
        self._fx: tuple[Decimal, float] | None = None

    # ───────────── 체결통보 수신 (공유 실시간 세션) ─────────────
    async def start(self) -> None:
        hts_id = self.account.secrets.hts_id.get_secret_value()
        for tr_id, market in self._notice_markets.items():
            if market in self.profile.markets:
                self.session.set_handler(tr_id, self._on_notice)
                self.session.desire(tr_id, hts_id, "notice")
        await self.session.start()

    async def stop(self) -> None:
        await self.session.stop()

    async def _reconnected(self) -> None:
        if self.on_reconnect:
            await self.on_reconnect()

    def health(self) -> dict:
        expires = self.auth.token_expires_at
        return {
            "connected": self.notice_connected.is_set(),
            "notice_connected": self.notice_connected.is_set(),
            "token_expires_at": expires.isoformat() if expires else None,
            "rate_limit_per_sec": self.profile.rate_limit_per_sec,
            "realtime": self.session.health(),
        }

    def call_stats(self) -> dict | None:
        return self.rest.stats.snapshot()

    async def _on_notice(self, msg: RealtimeMessage) -> None:
        market = self._notice_markets[msg.tr_id]
        parser = self._parsers[market]
        for record in msg.records:
            log.info("[%s] 체결통보 %s %s", self.account_id, market, parser.safe_view(parser.to_dict(record)))
            event = parser.parse(record, self.clock().date())
            if event is None:
                continue
            try:
                await self.emit(event)
            except Exception:
                log.exception("[%s] 체결통보 반영 실패: %s", self.account_id, event)

    # ───────────── 주문 ─────────────
    async def submit(self, req: BrokerOrderRequest) -> BrokerOrderAck:
        inst = req.instrument
        ap = self.rest.account_params()
        if inst.market == "KR":
            trx = tr.KR_ORDER_BUY if req.side == "buy" else tr.KR_ORDER_SELL
            params = {
                **ap, "PDNO": inst.code, "ORD_DVSN": "00" if req.order_type == "limit" else "01",
                "ORD_QTY": str(req.qty), "ORD_UNPR": _krw(req.price) if req.order_type == "limit" else "0",
                "EXCG_ID_DVSN_CD": req.route or "KRX", "SLL_TYPE": "01" if req.side == "sell" else "", "CNDT_PRIC": "",
            }
        else:
            trx = tr.US_ORDER_BUY if req.side == "buy" else tr.US_ORDER_SELL
            params = {
                **ap, "OVRS_EXCG_CD": inst.exchange, "PDNO": inst.code, "ORD_QTY": str(req.qty),
                "OVRS_ORD_UNPR": _usd(req.price), "CTAC_TLNO": "", "MGCO_APTM_ODNO": "",
                "SLL_TYPE": "00" if req.side == "sell" else "", "ORD_SVR_DVSN_CD": "0", "ORD_DVSN": "00",
            }
        out = await self._call(trx, params)
        return BrokerOrderAck(str(out.get("ODNO", "")), str(out.get("KRX_FWDG_ORD_ORGNO", "")))

    async def cancel(
        self, broker_order_no: str, broker_org_no: str, instrument: Instrument, qty: int, route: str | None = None
    ) -> None:
        ap = self.rest.account_params()
        if instrument.market == "KR":
            await self._call(tr.KR_ORDER_RVSECNCL, {
                **ap, "KRX_FWDG_ORD_ORGNO": broker_org_no, "ORGN_ODNO": broker_order_no, "ORD_DVSN": "00",
                "RVSE_CNCL_DVSN_CD": "02", "ORD_QTY": "0", "ORD_UNPR": "0", "QTY_ALL_ORD_YN": "Y",
                "EXCG_ID_DVSN_CD": route or "KRX",
            })
        else:
            await self._call(tr.US_ORDER_RVSECNCL, {
                **ap, "OVRS_EXCG_CD": instrument.exchange, "PDNO": instrument.code, "ORGN_ODNO": broker_order_no,
                "RVSE_CNCL_DVSN_CD": "02", "ORD_QTY": str(qty), "OVRS_ORD_UNPR": "0", "MGCO_APTM_ODNO": "",
                "ORD_SVR_DVSN_CD": "0",
            })

    async def amend(
        self, broker_order_no: str, broker_org_no: str, instrument: Instrument, price: Decimal, qty: int,
        route: str | None = None,
    ) -> BrokerOrderAck:
        ap = self.rest.account_params()
        if instrument.market == "KR":
            out = await self._call(tr.KR_ORDER_RVSECNCL, {
                **ap, "KRX_FWDG_ORD_ORGNO": broker_org_no, "ORGN_ODNO": broker_order_no, "ORD_DVSN": "00",
                "RVSE_CNCL_DVSN_CD": "01", "ORD_QTY": str(qty), "ORD_UNPR": _krw(price), "QTY_ALL_ORD_YN": "N",
                "EXCG_ID_DVSN_CD": route or "KRX",
            })
        else:
            out = await self._call(tr.US_ORDER_RVSECNCL, {
                **ap, "OVRS_EXCG_CD": instrument.exchange, "PDNO": instrument.code, "ORGN_ODNO": broker_order_no,
                "RVSE_CNCL_DVSN_CD": "01", "ORD_QTY": str(qty), "OVRS_ORD_UNPR": _usd(price), "MGCO_APTM_ODNO": "",
                "ORD_SVR_DVSN_CD": "0",
            })
        return BrokerOrderAck(str(out.get("ODNO", "")), str(out.get("KRX_FWDG_ORD_ORGNO", broker_org_no)))

    async def _call(self, trx: tr.Tr, params: dict) -> dict:
        """주문 계열 호출: 명확한 거부는 BrokerReject, 결과를 알 수 없으면 BrokerUnavailable."""
        try:
            resp = await self.rest.request(trx, params)
        except KisApiError as e:
            if e.msg_cd:
                raise BrokerReject(e.msg_cd, e.msg) from None
            raise BrokerUnavailable(str(e)) from None
        except (httpx.TimeoutException, httpx.TransportError, KisAuthError) as e:
            raise BrokerUnavailable(f"{type(e).__name__}: {e}") from None
        out = resp.body.get("output") or {}
        return out if isinstance(out, dict) else {}

    # ───────────── 시세 / 환율 ─────────────
    async def quote(self, instrument: Instrument) -> Quote:
        try:
            if instrument.market == "KR":
                code = "UN" if {"NXT", "SOR"} & set(self.profile.kr_routes) else "J"
                o = (await self.rest.request(tr.KR_PRICE, {"FID_COND_MRKT_DIV_CODE": code,
                                                           "FID_INPUT_ISCD": instrument.code})).body["output"]
                return Quote(_d(o.get("stck_prpr")), _d(o.get("stck_mxpr")) or None, _d(o.get("stck_llam")) or None)
            o = (await self.rest.request(tr.US_PRICE, {"AUTH": "", "EXCD": tr.US_QUOTE_EXCD[instrument.exchange],
                                                       "SYMB": instrument.code})).body["output"]
            return Quote(_d(o.get("last")))
        except (KisApiError, httpx.HTTPError, KeyError) as e:
            raise BrokerUnavailable(f"시세 조회 실패: {e}") from None

    async def fx_rate(self, pair: str = "USDKRW") -> Decimal:
        if self._fx and time.monotonic() - self._fx[1] < FX_TTL_SEC:
            return self._fx[0]
        if tr.US_FOREIGN_MARGIN.paper is None and self.profile.env == "paper":
            raise BrokerUnavailable("모의투자는 환율 조회 미지원")
        try:
            resp = await self.rest.request(tr.US_FOREIGN_MARGIN, self.rest.account_params())
        except (KisApiError, httpx.HTTPError) as e:
            raise BrokerUnavailable(f"환율 조회 실패: {e}") from None
        for row in _rows(resp.body, "output"):
            if row.get("crcy_cd") == "USD" and _d(row.get("bass_exrt")) > 0:
                rate = _d(row.get("bass_exrt"))
                self._fx = (rate, time.monotonic())
                return rate
        raise BrokerUnavailable("USD 기준환율 없음")

    # ───────────── 캔들 / 호가 ─────────────
    def _kr_mkt(self) -> str:
        return "UN" if {"NXT", "SOR"} & set(self.profile.kr_routes) else "J"

    async def candle_page(self, instrument: Instrument, interval: str, end: datetime) -> list[Candle]:
        """KIS 응답은 최신부터 오므로 뒤집어 오래된 순으로 돌려준다. 봉 시각은 시작 시각 (2026-10-14 실측)."""
        end = end.astimezone(KST)
        try:
            if instrument.market == "KR":
                return await self._kr_candles(instrument, interval, end)
            return await self._us_candles(instrument, interval, end)
        except (KisApiError, httpx.HTTPError) as e:
            raise BrokerUnavailable(f"캔들 조회 실패: {e}") from None

    async def _kr_candles(
        self, inst: Instrument, interval: str, end: datetime, mkt: str | None = None, adjusted: bool = True,
    ) -> list[Candle]:
        mkt = mkt or self._kr_mkt()
        if interval == "1m":
            body = (await self.rest.request(tr.KR_MINUTE_DAILY, {
                "FID_COND_MRKT_DIV_CODE": mkt, "FID_INPUT_ISCD": inst.code,
                "FID_INPUT_HOUR_1": end.strftime("%H%M%S"), "FID_INPUT_DATE_1": end.strftime("%Y%m%d"),
                "FID_PW_DATA_INCU_YN": "Y", "FID_FAKE_TICK_INCU_YN": ""})).body
            rows = [(datetime.strptime(r["stck_bsop_date"] + r["stck_cntg_hour"], "%Y%m%d%H%M%S").replace(tzinfo=KST),
                     r["stck_oprc"], r["stck_hgpr"], r["stck_lwpr"], r["stck_prpr"], r["cntg_vol"])
                    for r in _rows(body, "output2") if r.get("stck_bsop_date")]
        else:
            span = {"1d": 150, "1w": 1000, "1M": 4000}[interval]
            body = (await self.rest.request(tr.KR_DAILY_CHART, {
                "FID_COND_MRKT_DIV_CODE": mkt, "FID_INPUT_ISCD": inst.code,
                "FID_INPUT_DATE_1": (end - timedelta(days=span)).strftime("%Y%m%d"),
                "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
                "FID_PERIOD_DIV_CODE": {"1d": "D", "1w": "W", "1M": "M"}[interval],
                "FID_ORG_ADJ_PRC": "0" if adjusted else "1"})).body
            rows = [(datetime.strptime(r["stck_bsop_date"], "%Y%m%d").replace(tzinfo=KST),
                     r["stck_oprc"], r["stck_hgpr"], r["stck_lwpr"], r["stck_clpr"], r["acml_vol"])
                    for r in _rows(body, "output2") if r.get("stck_bsop_date")]
        return _candles(rows, end)

    async def _us_candles(self, inst: Instrument, interval: str, end: datetime, adjusted: bool = True) -> list[Candle]:
        excd = tr.US_QUOTE_EXCD[inst.exchange]
        if interval == "1m":
            first = end >= self.clock() - timedelta(minutes=1)
            ny_end = end.astimezone(NEW_YORK)
            body = (await self.rest.request(tr.US_MINUTE_CHART, {
                "AUTH": "", "EXCD": excd, "SYMB": inst.code, "NMIN": "1", "PINC": "1",
                "NEXT": "" if first else "1", "NREC": "120", "FILL": "",
                "KEYB": "" if first else ny_end.strftime("%Y%m%d%H%M%S")})).body
            # 시간외 봉도 그대로 돌려준다 (정규장 필터는 CandleService). 여기서 거르면 시간외만 있는 페이지가
            # 빈 페이지가 되어 연속조회가 끊긴다 (2026-10-14 실측)
            rows = [(datetime.strptime(r["kymd"] + r["khms"], "%Y%m%d%H%M%S").replace(tzinfo=KST),
                     r["open"], r["high"], r["low"], r["last"], r["evol"])
                    for r in _rows(body, "output2") if r.get("kymd")]
        else:
            body = (await self.rest.request(tr.US_DAILY_CHART, {
                "AUTH": "", "EXCD": excd, "SYMB": inst.code, "GUBN": {"1d": "0", "1w": "1", "1M": "2"}[interval],
                "BYMD": end.astimezone(NEW_YORK).strftime("%Y%m%d"), "MODP": "1" if adjusted else "0"})).body
            # 휴장일에 거래량 0, 시가=고가=저가=종가인 채움 행이 섞여 온다 (2010 년 등, 2026-10-15 실측)
            rows = [(datetime.strptime(r["xymd"], "%Y%m%d").replace(tzinfo=KST),
                     r["open"], r["high"], r["low"], r["clos"], r["tvol"])
                    for r in _rows(body, "output2")
                    if r.get("xymd") and not (_i(r.get("tvol")) == 0 and len({r["open"], r["high"], r["low"], r["clos"]}) == 1)]
        return _candles(rows, end)

    # ───────────── 과거 시세 보관 (central/marketdata/history.py) ─────────────
    async def history_minutes(self, inst: Instrument, end: datetime) -> list[Candle]:
        """보관용 1분봉 한 페이지 (시간외 포함, 오래된 순). 국내는 KRX(J) 시세로 고정한다."""
        try:
            if inst.market == "KR":
                return await self._kr_candles(inst, "1m", end.astimezone(KST), mkt="J")
            return await self._us_candles(inst, "1m", end.astimezone(KST))
        except (KisApiError, httpx.HTTPError) as e:
            raise BrokerUnavailable(f"분봉 조회 실패: {e}") from None

    async def history_daily(self, inst: Instrument, end: date, adjusted: bool) -> list[Candle]:
        """보관용 일봉 한 페이지 (end 이전 최대 100개). adjusted=False 면 원주가."""
        # 현지 날짜의 하루 끝: 미국 BYMD 는 뉴욕 날짜로 바꿔 보내므로 KST 0시를 쓰면 하루 앞당겨진다
        at = datetime.combine(end, datetime.max.time().replace(microsecond=0), KST if inst.market == "KR" else NEW_YORK)
        try:
            if inst.market == "KR":
                return await self._kr_candles(inst, "1d", at, mkt="J", adjusted=adjusted)
            return await self._us_candles(inst, "1d", at, adjusted=adjusted)
        except (KisApiError, httpx.HTTPError) as e:
            raise BrokerUnavailable(f"일봉 조회 실패: {e}") from None

    async def index_daily(self, market: str, code: str, end: date) -> list[Candle]:
        """지수 일봉 한 페이지 (국내 최대 50개, 해외 최대 100개). 국내 code 예: 0001 코스피, 해외 예: COMP, SPX."""
        try:
            if market == "KR":
                body = (await self.rest.request(tr.KR_INDEX_DAILY, {
                    "FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": code,
                    # 구간 안의 최근 50개만 오므로 시작일을 넉넉히 잡아 매번 50개를 채운다
                    "FID_INPUT_DATE_1": (end - timedelta(days=3650)).strftime("%Y%m%d"),
                    "FID_INPUT_DATE_2": end.strftime("%Y%m%d"), "FID_PERIOD_DIV_CODE": "D"})).body
                rows = [(datetime.strptime(r["stck_bsop_date"], "%Y%m%d").replace(tzinfo=KST), r["bstp_nmix_oprc"],
                         r["bstp_nmix_hgpr"], r["bstp_nmix_lwpr"], r["bstp_nmix_prpr"], r["acml_vol"])
                        for r in _rows(body, "output2") if r.get("stck_bsop_date")]
            else:
                body = (await self.rest.request(tr.OVERSEAS_INDEX_DAILY, {
                    "FID_COND_MRKT_DIV_CODE": "N", "FID_INPUT_ISCD": code,
                    "FID_INPUT_DATE_1": (end - timedelta(days=3650)).strftime("%Y%m%d"),
                    "FID_INPUT_DATE_2": end.strftime("%Y%m%d"), "FID_PERIOD_DIV_CODE": "D"})).body
                rows = [(datetime.strptime(r["stck_bsop_date"], "%Y%m%d").replace(tzinfo=KST), r["ovrs_nmix_oprc"],
                         r["ovrs_nmix_hgpr"], r["ovrs_nmix_lwpr"], r["ovrs_nmix_prpr"], r["acml_vol"])
                        for r in _rows(body, "output2") if r.get("stck_bsop_date")]
        except (KisApiError, httpx.HTTPError) as e:
            raise BrokerUnavailable(f"지수 일봉 조회 실패: {e}") from None
        return _candles(rows, datetime.combine(end, datetime.min.time(), KST))

    async def index_minutes(self, market: str, code: str) -> list[Candle]:
        """지수 1분봉: 최근 약 100개만 온다 (날짜 지정, 연속조회 없음. 2026-10-15 실측). 장중에 주기적으로 받아 쌓는다."""
        try:
            if market == "KR":
                body = (await self.rest.request(tr.KR_INDEX_MINUTE, {
                    "FID_COND_MRKT_DIV_CODE": "U", "FID_ETC_CLS_CODE": "0", "FID_INPUT_ISCD": code,
                    "FID_INPUT_HOUR_1": "60", "FID_PW_DATA_INCU_YN": "Y"})).body
                rows = [(datetime.strptime(r["stck_bsop_date"] + r["stck_cntg_hour"], "%Y%m%d%H%M%S").replace(tzinfo=KST),
                         r["bstp_nmix_oprc"], r["bstp_nmix_hgpr"], r["bstp_nmix_lwpr"], r["bstp_nmix_prpr"], r["cntg_vol"])
                        for r in _rows(body, "output2")
                        if r.get("stck_bsop_date") and r.get("stck_cntg_hour", "999999") < "240000"]  # 999999 요약 행 제외
            else:
                body = (await self.rest.request(tr.OVERSEAS_INDEX_MINUTE, {
                    "FID_COND_MRKT_DIV_CODE": "N", "FID_INPUT_ISCD": code, "FID_HOUR_CLS_CODE": "0",
                    "FID_PW_DATA_INCU_YN": "Y"})).body
                # 해외지수 분봉 시각은 뉴욕 현지 시각
                rows = [(datetime.strptime(r["stck_bsop_date"] + r["stck_cntg_hour"], "%Y%m%d%H%M%S")
                         .replace(tzinfo=NEW_YORK).astimezone(KST),
                         r["optn_oprc"], r["optn_hgpr"], r["optn_lwpr"], r["optn_prpr"], r["cntg_vol"])
                        for r in _rows(body, "output2") if r.get("stck_bsop_date")]
        except (KisApiError, httpx.HTTPError) as e:
            raise BrokerUnavailable(f"지수 분봉 조회 실패: {e}") from None
        return _candles(rows, now_kst() + timedelta(days=1))

    async def market_days(self, start: date, pages: int = 3) -> list[tuple[date, bool]]:
        """국내 개장일 (CTCA0903R, 실전 전용). 1회 24일치, 같은 기준일에 ctx_area_fk/nk 와 tr_cont=N 으로 이어 받는다."""
        if tr.KR_HOLIDAY.paper is None and self.profile.env == "paper":
            raise BrokerUnavailable("모의투자는 휴장일 조회 미지원 (EGW02006)")
        out: list[tuple[date, bool]] = []
        fk = nk = tr_cont = ""
        try:
            for _ in range(pages):
                resp = await self.rest.request(tr.KR_HOLIDAY, {
                    "BASS_DT": start.strftime("%Y%m%d"), "CTX_AREA_FK": fk, "CTX_AREA_NK": nk}, tr_cont=tr_cont)
                out += [(datetime.strptime(r["bass_dt"], "%Y%m%d").date(), r.get("opnd_yn") == "Y")
                        for r in _rows(resp.body, "output") if r.get("bass_dt")]
                if not resp.has_next:
                    break
                fk, nk, tr_cont = resp.body.get("ctx_area_fk", ""), resp.body.get("ctx_area_nk", ""), "N"
        except (KisApiError, httpx.HTTPError) as e:
            raise BrokerUnavailable(f"휴장일 조회 실패: {e}") from None
        return out

    async def dataset(self, query) -> list[dict]:
        from central.broker.kis import datasets

        trx, params, key = datasets.request(query)
        try:
            body = (await self.rest.request(trx, params)).body
        except (KisApiError, httpx.HTTPError) as e:
            raise BrokerUnavailable(f"{query.group}/{query.kind} 조회 실패: {e}") from None
        return datasets.rows(query, [r for r in _rows(body, key) if r])

    async def orderbook(self, instrument: Instrument) -> OrderBook:
        try:
            if instrument.market == "KR":
                o = (await self.rest.request(tr.KR_ASKING_PRICE, {
                    "FID_COND_MRKT_DIV_CODE": self._kr_mkt(), "FID_INPUT_ISCD": instrument.code})).body["output1"]
                asks = [(_d(o[f"askp{i}"]), _i(o[f"askp_rsqn{i}"])) for i in range(1, 11)]
                bids = [(_d(o[f"bidp{i}"]), _i(o[f"bidp_rsqn{i}"])) for i in range(1, 11)]
            else:
                o = (await self.rest.request(tr.US_ASKING_PRICE, {
                    "AUTH": "", "EXCD": tr.US_QUOTE_EXCD[instrument.exchange], "SYMB": instrument.code})).body["output2"]
                asks = [(_d(o[f"pask{i}"]), _i(o[f"vask{i}"])) for i in range(1, 11)]
                bids = [(_d(o[f"pbid{i}"]), _i(o[f"vbid{i}"])) for i in range(1, 11)]
        except (KisApiError, httpx.HTTPError, KeyError) as e:
            raise BrokerUnavailable(f"호가 조회 실패: {e}") from None
        return OrderBook(self.clock(), [x for x in asks if x[0] > 0], [x for x in bids if x[0] > 0])

    # ───────────── 주문내역 / 잔고 ─────────────
    async def order_statuses(self, market: str, day: date) -> list[BrokerOrderStatus]:
        ap = self.rest.account_params()
        if market == "KR":
            pages = await self.rest.paginate(tr.KR_DAILY_CCLD, {
                **ap, "INQR_STRT_DT": f"{day:%Y%m%d}", "INQR_END_DT": f"{day:%Y%m%d}", "SLL_BUY_DVSN_CD": "00",
                "PDNO": "", "CCLD_DVSN": "00", "INQR_DVSN": "00", "INQR_DVSN_3": "00", "ORD_GNO_BRNO": "",
                "ODNO": "", "INQR_DVSN_1": "", "EXCG_ID_DVSN_CD": "ALL" if "SOR" in self.profile.kr_routes else "KRX",
            }, PAGE_100)
            rows = [r for p in pages for r in _rows(p.body, "output1") if r.get("odno")]
            return [
                BrokerOrderStatus(
                    broker_order_no=r["odno"], instrument_id=f"KR:{r.get('pdno', '')}",
                    side="sell" if r.get("sll_buy_dvsn_cd") == "01" else "buy",
                    qty=_i(r.get("ord_qty")), price=_d(r.get("ord_unpr")) or None,
                    filled_qty=_i(r.get("tot_ccld_qty")), avg_fill_price=_d(r.get("avg_prvs")) or None,
                    open_qty=_i(r.get("rmn_qty")), orig_order_no=(r.get("orgn_odno") or "").strip() or None,
                    ordered_at=str(r.get("ord_tmd", "")),
                ) for r in rows
            ]
        start = day - timedelta(days=1)  # 미국 주문일자 기준(현지/한국) 차이를 흡수
        pages = await self.rest.paginate(tr.US_CCNL, {
            **ap, "PDNO": "%", "ORD_STRT_DT": f"{start:%Y%m%d}", "ORD_END_DT": f"{day:%Y%m%d}",
            "SLL_BUY_DVSN": "00", "CCLD_NCCS_DVSN": "00", "OVRS_EXCG_CD": "%", "SORT_SQN": "DS",
            "ORD_DT": "", "ORD_GNO_BRNO": "", "ODNO": "",
        }, PAGE_200)
        rows = [r for p in pages for r in _rows(p.body, "output") if r.get("odno")]
        return [
            BrokerOrderStatus(
                broker_order_no=r["odno"], instrument_id=f"US:{r.get('pdno', '')}",
                side="sell" if r.get("sll_buy_dvsn_cd") == "01" else "buy",
                qty=_i(r.get("ft_ord_qty")), price=_d(r.get("ft_ord_unpr3")) or None,
                filled_qty=_i(r.get("ft_ccld_qty")), avg_fill_price=_d(r.get("ft_ccld_unpr3")) or None,
                open_qty=_i(r.get("nccs_qty")), orig_order_no=(r.get("orgn_odno") or "").strip() or None,
                ordered_at=str(r.get("ord_tmd", "")),
            ) for r in rows
        ]

    async def cash_rights(self, market: str, day: date) -> list[CashRight]:
        """국내: 기간별계좌권리현황(CTRGA011R)에서 현금지급일이 day 이고 금액이 있는 행. 미국과 모의는 조회 수단 없음.
        필드 의미(최종배정금액=세전, 세금금액=원천징수)는 실제 배당 지급 때 재확인 필요: 대사는 금액이
        현금 차이와 정확히 맞을 때만 배당으로 인정하므로 해석이 틀리면 미확인 입금으로 남는다."""
        if market != "KR" or self.account.profile.env != "real":
            return []
        ymd = f"{day:%Y%m%d}"
        pages = await self.rest.paginate(tr.KR_PERIOD_RIGHTS, {
            **self.rest.account_params(), "INQR_DVSN": "03", "INQR_STRT_DT": ymd, "INQR_END_DT": ymd,
            "CUST_RNCNO25": "", "HMID": "", "RGHT_TYPE_CD": "", "PDNO": "", "PRDT_TYPE_CD": ""}, PAGE_100)
        out = []
        for r in (r for p in pages for r in _rows(p.body, "output")):
            amount = _d(r.get("last_alct_amt"))
            if r.get("cash_dfrm_dt") != ymd or amount <= 0:
                continue
            log.info("[%s] 권리 현금 지급 %s", self.account_id,
                     {k: v for k, v in r.items() if k != "acno10" and v not in ("", None)})
            code = (r.get("shtn_pdno") or r.get("pdno") or "").strip()
            bass = r.get("bass_dt") or ""
            out.append(CashRight(f"KR:{code[-6:]}", datetime.strptime(bass, "%Y%m%d").date() if len(bass) == 8 else None,
                                 _i(r.get("cblc_qty")), amount, _d(r.get("tax_amt")), r.get("rght_type_cd") or "dividend"))
        return out

    async def snapshot(self, market: str) -> AccountSnapshot:
        ap = self.rest.account_params()
        if market == "KR":
            pages = await self.rest.paginate(tr.KR_BALANCE, {
                **ap, "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00",
            }, PAGE_100)
            positions = {
                f"KR:{r['pdno']}": (_i(r.get("hldg_qty")), _d(r.get("pchs_avg_pric")))
                for p in pages for r in _rows(p.body, "output1") if r.get("pdno") and _i(r.get("hldg_qty"))
            }
            summary = _rows(pages[0].body, "output2")
            cash = {"KRW": _d(summary[0].get("prvs_rcdl_excc_amt"))} if summary else {}
            return AccountSnapshot("KR", cash, positions)

        resp = await self.rest.request(tr.US_PRESENT_BALANCE, {
            **ap, "WCRC_FRCR_DVSN_CD": "02", "NATN_CD": "840", "TR_MKET_CD": "00", "INQR_DVSN_CD": "00",
        })
        positions = {
            f"US:{r['pdno']}": (_i(r.get("ccld_qty_smtl1")), _d(r.get("avg_unpr3")))
            for r in _rows(resp.body, "output1") if r.get("pdno") and _i(r.get("ccld_qty_smtl1"))
        }
        cash: dict[str, Decimal] = {}
        fx = None
        if not (self.profile.env == "paper" and tr.US_FOREIGN_MARGIN.paper is None):
            margin = await self.rest.request(tr.US_FOREIGN_MARGIN, ap)
            for row in _rows(margin.body, "output"):
                if row.get("crcy_cd") == "USD":
                    # 외화예수금 - 미결제매수 + 미결제매도 = 매매일 기준 USD  (실데이터로 재확인 필요)
                    cash["USD"] = _d(row.get("frcr_dncl_amt1")) - _d(row.get("ustl_buy_amt")) + _d(row.get("ustl_sll_amt"))
                    fx = _d(row.get("bass_exrt")) or None
        return AccountSnapshot("US", cash, positions, fx)


def _krw(price: Decimal | None) -> str:
    if price is None:
        return "0"
    return str(int(price))


def _usd(price: Decimal | None) -> str:
    if price is None:
        return "0"
    return format(price.normalize(), "f")
