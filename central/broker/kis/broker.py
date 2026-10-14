"""KisBroker: Broker 인터페이스의 한국투자증권 구현 (모의/실전 공용).

- 주문/정정/취소/시세/주문내역/잔고: REST (central/broker/kis/rest.py)
- 체결통보: 전용 WebSocket 세션. 끊기면 지수 백오프로 재연결하고, 재연결 직후 on_reconnect 콜백으로
  주문 동기화(central/oms/sync.py)를 돌려 끊긴 동안의 체결을 보정한다.
"""

from __future__ import annotations

import asyncio
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
    Quote,
)
from central.broker.kis import tr
from central.broker.kis.auth import KisAuth, KisAuthError
from central.broker.kis.notice import NoticeParser
from central.broker.kis.rest import PAGE_100, PAGE_200, KisApiError, KisRestClient
from central.broker.kis.ws import KisWebSocket
from central.core.instruments import Instrument
from central.core.market_hours import now_kst
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
        self.notice_connected = asyncio.Event()
        env = self.profile.env
        self._notice_markets = {
            tr.WS_KR_NOTICE[env]: "KR", tr.WS_US_NOTICE[env]: "US",
        }
        self._parsers = {"KR": NoticeParser("KR"), "US": NoticeParser("US")}
        self._task: asyncio.Task | None = None
        self._fx: tuple[Decimal, float] | None = None

    # ───────────── 체결통보 수신 ─────────────
    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._notice_loop(), name=f"kis-notice-{self.account_id}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def health(self) -> dict:
        expires = self.auth.token_expires_at
        return {
            "connected": self.notice_connected.is_set(),
            "notice_connected": self.notice_connected.is_set(),
            "token_expires_at": expires.isoformat() if expires else None,
            "rate_limit_per_sec": self.profile.rate_limit_per_sec,
            "ws_max_subscriptions": self.profile.ws_max_subscriptions,
        }

    async def _notice_loop(self) -> None:
        backoff, connects = 1.0, 0
        hts_id = self.account.secrets.hts_id.get_secret_value()
        while True:
            ws = KisWebSocket(self.auth)
            try:
                await ws.connect()
                for tr_id, market in self._notice_markets.items():
                    if market in self.profile.markets:
                        ack = await ws.subscribe(tr_id, hts_id)
                        if not ack.ok:
                            raise KisAuthError(f"체결통보 구독 실패 {tr_id}: [{ack.msg_cd}] {ack.msg}")
                connects += 1
                backoff = 1.0
                self.notice_connected.set()
                log.info("[%s] 체결통보 수신 시작 (%d회차 연결)", self.account_id, connects)
                if connects > 1 and self.on_reconnect:
                    await self.on_reconnect()
                await self._consume(ws)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("[%s] 체결통보 연결 오류", self.account_id)
            finally:
                self.notice_connected.clear()
                await ws.close()
            log.warning("[%s] 체결통보 재연결 대기 %.0f초", self.account_id, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def _consume(self, ws: KisWebSocket) -> None:
        while not ws.closed.is_set():
            try:
                msg = await asyncio.wait_for(ws.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            market = self._notice_markets.get(msg.tr_id)
            if market is None:
                continue
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
