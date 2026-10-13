"""KIS 연결 점검 도구 (M1).

    python -m tools.kis_check                       # 모든 실계좌, 조회 계열만
    python -m tools.kis_check --account paper --market kr
    python -m tools.kis_check --account paper --order-test   # 모의 주문/취소 (paper 전용)

- 계좌 profile 의 capability(markets, kr_routes, integrated_margin)에 맞는 항목만 실행한다.
- 출력에는 금액/계좌번호/키를 포함하지 않는다. 상세 JSON 은 CENTRAL_HOME/logs 에 저장한다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Awaitable, Callable

import httpx

from central.broker.kis import tr
from central.broker.kis.auth import KST, KisAuth, KisAuthError
from central.broker.kis.rest import PAGE_100, PAGE_200, KisApiError, KisRestClient
from central.broker.kis.ws import KisWebSocket, KisWsError
from central.core.logging import masker, setup_logging
from central.settings import Account, Settings, SettingsError, load_settings

KR_SYMBOL = "005930"
US_EXCHANGE, US_SYMBOL = "NASD", "AAPL"


@dataclass
class Result:
    account: str
    id: str
    name: str
    status: str  # OK / WARN / FAIL / SKIP
    detail: str = ""
    ms: float = 0.0


class Checker:
    def __init__(self, settings: Settings, account: Account, http: httpx.AsyncClient, ws_timeout: float) -> None:
        self.settings = settings
        self.account = account
        self.p = account.profile
        self.auth = KisAuth(account, settings.tokens_dir, http)
        self.rest = KisRestClient(self.auth, http)
        self.http = http
        self.ws_timeout = ws_timeout
        self.results: list[Result] = []

    # ───────────── 실행 헬퍼 ─────────────
    async def run(self, id_: str, name: str, fn: Callable[[], Awaitable[str | tuple[str, str]]]) -> None:
        started = time.monotonic()
        try:
            out = await fn()
            status, detail = out if isinstance(out, tuple) else ("OK", out)
        except KisApiError as e:
            status, detail = "FAIL", f"[{e.msg_cd}] {e.msg} (HTTP {e.http_status})"
        except (KisAuthError, KisWsError) as e:
            status, detail = "FAIL", str(e)
        except asyncio.TimeoutError:
            status, detail = "FAIL", "응답 시간 초과"
        except Exception as e:  # 점검 도구이므로 예외를 결과로 기록하고 계속 진행
            status, detail = "FAIL", f"{type(e).__name__}: {e}"
        ms = (time.monotonic() - started) * 1000
        r = Result(self.account.alias, id_, name, status, masker.mask(detail), ms)
        self.results.append(r)
        print(f"  [{r.status:4}] {r.id:4} {r.name:<28} {r.detail}  ({r.ms:.0f}ms)", flush=True)

    def skip(self, id_: str, name: str, reason: str) -> None:
        self.results.append(Result(self.account.alias, id_, name, "SKIP", reason))
        print(f"  [SKIP] {id_:4} {name:<28} {reason}", flush=True)

    # ───────────── 공통 ─────────────
    async def common(self) -> None:
        async def token() -> str:
            await self.auth.access_token()
            t = self.auth.cached_token()
            how = "신규 발급" if self.auth.last_issue_was_new else "캐시 사용"
            return f"{how}, 만료 {t.expires_at:%m-%d %H:%M}" if t else how

        async def cache_reuse() -> str | tuple[str, str]:
            other = KisAuth(self.account, self.settings.tokens_dir, self.http)
            t = other.cached_token()
            if t and t.usable(datetime.now(KST)):
                return "재실행 시 재발급 없이 사용 가능"
            return "FAIL", "캐시된 토큰 없음 또는 만료"

        async def approval() -> str:
            await self.auth.approval_key()
            return "발급 성공"

        await self.run("C1", "접근토큰", token)
        await self.run("C2", "토큰 캐시 재사용", cache_reuse)
        await self.run("C3", "WS 접속키", approval)

    async def rate_limit(self) -> None:
        n = max(4, min(int(self.p.rate_limit_per_sec * 2), 20))

        async def burst() -> str | tuple[str, str]:
            started = time.monotonic()
            calls = [
                self.rest.request(tr.KR_PRICE, {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": KR_SYMBOL})
                for _ in range(n)
            ]
            await asyncio.gather(*calls)
            elapsed = time.monotonic() - started
            return f"{n}건 동시 요청 → 모두 성공, {elapsed:.1f}초 ({n / elapsed:.1f}건/초, 설정 {self.p.rate_limit_per_sec}/초)"

        await self.run("C4", "호출 속도 제한", burst)

    # ───────────── 국내 ─────────────
    async def kr(self) -> None:
        today = datetime.now(KST)
        markets = [("J", "KRX")] + ([("UN", "통합")] if {"NXT", "SOR"} & set(self.p.kr_routes) else [])

        for code, label in markets:
            async def price(code: str = code) -> str:
                r = await self.rest.request(tr.KR_PRICE, {"FID_COND_MRKT_DIV_CODE": code, "FID_INPUT_ISCD": KR_SYMBOL})
                return f"{KR_SYMBOL} 현재가 {r.body['output']['stck_prpr']}"

            await self.run("K1", f"현재가({label})", price)

        async def daily() -> str:
            r = await self.rest.request(tr.KR_DAILY_CHART, {
                "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": KR_SYMBOL,
                "FID_INPUT_DATE_1": (today - timedelta(days=200)).strftime("%Y%m%d"),
                "FID_INPUT_DATE_2": today.strftime("%Y%m%d"),
                "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0",
            })
            rows = [x for x in r.body.get("output2", []) if x.get("stck_bsop_date")]
            return f"{len(rows)}건, 최근 {rows[0]['stck_bsop_date'] if rows else '-'}"

        async def minute_today() -> str:
            r = await self.rest.request(tr.KR_MINUTE_TODAY, {
                "FID_ETC_CLS_CODE": "", "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": KR_SYMBOL,
                "FID_INPUT_HOUR_1": "153000", "FID_PW_DATA_INCU_YN": "Y",
            })
            rows = r.body.get("output2", [])
            return f"{len(rows)}건, 마지막 {rows[0].get('stck_bsop_date', '')} {rows[0].get('stck_cntg_hour', '')}" if rows else "0건"

        async def minute_past() -> str:
            r = await self.rest.request(tr.KR_MINUTE_DAILY, {
                "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": KR_SYMBOL,
                "FID_INPUT_HOUR_1": "130000",
                "FID_INPUT_DATE_1": (today - timedelta(days=30)).strftime("%Y%m%d"),
                "FID_PW_DATA_INCU_YN": "N", "FID_FAKE_TICK_INCU_YN": "",
            })
            rows = r.body.get("output2", [])
            return f"{len(rows)}건, 첫 행 {rows[0].get('stck_bsop_date', '')} {rows[0].get('stck_cntg_hour', '')}" if rows else "0건"

        async def balance() -> str:
            pages = await self.rest.paginate(tr.KR_BALANCE, {
                **self.rest.account_params(),
                "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00",
            }, PAGE_100)
            held = [x for p in pages for x in p.body.get("output1", []) if int(x.get("hldg_qty", "0") or 0) > 0]
            return f"조회 성공, 보유 {len(held)}종목, {len(pages)}페이지"

        async def psbl_order() -> str:
            await self.rest.request(tr.KR_PSBL_ORDER, {
                **self.rest.account_params(), "PDNO": KR_SYMBOL, "ORD_UNPR": "",
                "ORD_DVSN": "01", "CMA_EVLU_AMT_ICLD_YN": "N", "OVRS_ICLD_YN": "N",
            })
            return "조회 성공"

        async def holiday() -> str:
            r = await self.rest.request(tr.KR_HOLIDAY, {"BASS_DT": today.strftime("%Y%m%d"), "CTX_AREA_NK": "", "CTX_AREA_FK": ""})
            rows = r.body.get("output", [])
            first = rows[0] if rows else {}
            return f"{len(rows)}일 조회, 오늘 개장일={first.get('opnd_yn', '?')}"

        async def intgr_margin() -> str:
            await self.rest.request(tr.KR_INTGR_MARGIN, {
                **self.rest.account_params(), "CMA_EVLU_AMT_ICLD_YN": "N",
                "WCRC_FRCR_DVSN_CD": "01", "FWEX_CTRT_FRCR_DVSN_CD": "01",
            })
            return "조회 성공"

        await self.run("K2", "일봉", daily)
        await self.run("K3", "당일 분봉", minute_today)
        await self.run("K3b", "과거 분봉(일별)", minute_past)
        await self.run("K4", "잔고", balance)
        await self.run("K5", "매수가능", psbl_order)
        if self.p.env == "paper" and tr.KR_HOLIDAY.paper is None:
            self.skip("K6", "휴장일", "모의투자 미지원 TR (실전 계좌로 조회)")
        else:
            await self.run("K6", "휴장일", holiday)
        if self.p.integrated_margin:
            await self.run("K7", "통합증거금 현황", intgr_margin)
        else:
            self.skip("K7", "통합증거금 현황", "integrated_margin=false")

    # ───────────── 미국 ─────────────
    async def us(self) -> None:
        excd = tr.US_QUOTE_EXCD[US_EXCHANGE]

        async def price() -> str:
            r = await self.rest.request(tr.US_PRICE, {"AUTH": "", "EXCD": excd, "SYMB": US_SYMBOL})
            return f"{US_SYMBOL} 현재가 {r.body['output'].get('last', '?')}"

        async def daily() -> str:
            r = await self.rest.request(tr.US_DAILY_CHART, {
                "AUTH": "", "EXCD": excd, "SYMB": US_SYMBOL, "GUBN": "0", "BYMD": "", "MODP": "1",
            })
            rows = [x for x in r.body.get("output2", []) if x.get("xymd")]
            return f"{len(rows)}건, 최근 {rows[0]['xymd'] if rows else '-'}"

        async def minute() -> str:
            r = await self.rest.request(tr.US_MINUTE_CHART, {
                "AUTH": "", "EXCD": excd, "SYMB": US_SYMBOL, "NMIN": "1", "PINC": "1",
                "NEXT": "", "NREC": "120", "FILL": "", "KEYB": "",
            })
            rows = r.body.get("output2", [])
            return f"{len(rows)}건, 최근 {rows[0].get('kymd', '')} {rows[0].get('khms', '')} (KST)" if rows else "0건"

        async def balance() -> str:
            pages = await self.rest.paginate(tr.US_BALANCE, {
                **self.rest.account_params(), "OVRS_EXCG_CD": US_EXCHANGE, "TR_CRCY_CD": "USD",
            }, PAGE_200)
            held = [x for p in pages for x in p.body.get("output1", [])]
            return f"조회 성공, 보유 {len(held)}종목"

        async def present_balance() -> str:
            await self.rest.request(tr.US_PRESENT_BALANCE, {
                **self.rest.account_params(), "WCRC_FRCR_DVSN_CD": "02",
                "NATN_CD": "840", "TR_MKET_CD": "00", "INQR_DVSN_CD": "00",
            })
            return "조회 성공"

        async def psamount() -> str:
            await self.rest.request(tr.US_PSAMOUNT, {
                **self.rest.account_params(), "OVRS_EXCG_CD": US_EXCHANGE,
                "OVRS_ORD_UNPR": "1", "ITEM_CD": US_SYMBOL,
            })
            return "조회 성공"

        async def foreign_margin() -> str:
            await self.rest.request(tr.US_FOREIGN_MARGIN, self.rest.account_params())
            return "조회 성공"

        await self.run("U1", "현재가", price)
        await self.run("U2", "일봉", daily)
        await self.run("U3", "분봉", minute)
        await self.run("U4", "잔고", balance)
        await self.run("U4b", "체결기준 현재잔고", present_balance)
        await self.run("U5", "매수가능", psamount)
        if self.p.integrated_margin:
            await self.run("U6", "통화별 증거금", foreign_margin)
        else:
            self.skip("U6", "통화별 증거금", "integrated_margin=false")

    # ───────────── 실시간 ─────────────
    async def realtime(self, markets: list[str]) -> None:
        ws = KisWebSocket(self.auth)
        hts_id = self.account.secrets.hts_id.get_secret_value()
        env = self.p.env
        quotes: list[tuple[str, str, str, str]] = []  # (id, name, tr_id, tr_key)
        notices: list[tuple[str, str, str]] = []
        if "KR" in markets:
            quotes.append(("W1", "국내 체결가(KRX)", tr.WS_KR_TICK_KRX, KR_SYMBOL))
            if {"NXT", "SOR"} & set(self.p.kr_routes):
                quotes.append(("W2", "국내 체결가(통합)", tr.WS_KR_TICK_UNIFIED, KR_SYMBOL))
                quotes.append(("W3", "국내 호가(통합)", tr.WS_KR_ORDERBOOK_UNIFIED, KR_SYMBOL))
            else:
                quotes.append(("W3", "국내 호가(KRX)", tr.WS_KR_ORDERBOOK_KRX, KR_SYMBOL))
            notices.append(("W4", "국내 체결통보", tr.WS_KR_NOTICE[env]))
        if "US" in markets:
            key = tr.us_ws_key(US_EXCHANGE, US_SYMBOL)
            quotes.append(("W5", "미국 체결가", tr.WS_US_TICK, key))
            quotes.append(("W6", "미국 호가(1호가)", tr.WS_US_ORDERBOOK, key))
            notices.append(("W7", "미국 체결통보", tr.WS_US_NOTICE[env]))

        async def connect() -> str:
            await ws.connect()
            return "연결 성공"

        await self.run("W0", "WS 연결", connect)
        if ws._ws is None:
            return
        try:
            for id_, name, tr_id in notices:
                async def notice(tr_id: str = tr_id) -> str | tuple[str, str]:
                    ack = await ws.subscribe(tr_id, hts_id)
                    if not ack.ok:
                        return "FAIL", f"[{ack.msg_cd}] {ack.msg}"
                    has_key = tr_id in ws._ciphers
                    return f"구독 성공({ack.msg}), 복호화 키 {'수신' if has_key else '없음'}"

                await self.run(id_, name, notice)

            # 구독 한도가 작을 수 있으므로 시세는 한 슬롯으로 돌아가며 구독 → 수신 대기 → 해제
            per_quote = max(5.0, self.ws_timeout / max(len(quotes), 1))
            for id_, name, tr_id, key in quotes:
                async def quote(tr_id: str = tr_id, key: str = key) -> str | tuple[str, str]:
                    if ws.free_slots < 1:
                        return "SKIP", f"구독 슬롯 없음 (ws_max_subscriptions={self.p.ws_max_subscriptions})"
                    ack = await ws.subscribe(tr_id, key)
                    if not ack.ok:
                        return "FAIL", f"[{ack.msg_cd}] {ack.msg}"
                    try:
                        records, fields = await self._collect(ws, tr_id, per_quote)
                    finally:
                        await ws.unsubscribe(tr_id, key)
                    if not records:
                        return "WARN", f"구독 성공, {per_quote:.0f}초 내 데이터 없음 (장 운영시간 확인)"
                    return f"구독 성공, {records}건 수신, 필드 {fields}개"

                await self.run(id_, name, quote)
        finally:
            await ws.close()

    @staticmethod
    async def _collect(ws: KisWebSocket, tr_id: str, seconds: float) -> tuple[int, int]:
        """tr_id 데이터를 seconds 동안(또는 3초 연속 수신 시까지) 모아 (건수, 필드 수) 반환."""
        records = fields = 0
        deadline = time.monotonic() + seconds
        first_at: float | None = None
        while time.monotonic() < deadline:
            if first_at is not None and time.monotonic() - first_at > 3:
                break
            try:
                msg = await asyncio.wait_for(ws.queue.get(), timeout=max(0.1, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                break
            if msg.tr_id != tr_id:
                continue
            first_at = first_at or time.monotonic()
            records += len(msg.records)
            fields = len(msg.records[0]) if msg.records else 0
        return records, fields

    # ───────────── 주문 (paper 전용) ─────────────
    async def kr_order_test(self) -> None:
        if self.p.env != "paper":
            self.skip("O1", "국내 주문/취소", "실전 계좌 주문 테스트는 이 도구에서 실행하지 않음")
            return
        ws = KisWebSocket(self.auth)
        await ws.connect()
        notice_tr = tr.WS_KR_NOTICE[self.p.env]
        try:
            await ws.subscribe(notice_tr, self.account.secrets.hts_id.get_secret_value())
            state: dict[str, str] = {}

            async def order() -> str:
                r = await self.rest.request(tr.KR_PRICE, {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": KR_SYMBOL})
                lower = r.body["output"]["stck_llam"]  # 하한가: 체결되지 않을 가격
                o = await self.rest.request(tr.KR_ORDER_BUY, {
                    **self.rest.account_params(), "PDNO": KR_SYMBOL, "ORD_DVSN": "00",
                    "ORD_QTY": "1", "ORD_UNPR": lower, "EXCG_ID_DVSN_CD": "KRX", "SLL_TYPE": "", "CNDT_PRIC": "",
                })
                out = o.body["output"]
                state["odno"], state["orgno"] = out["ODNO"], out["KRX_FWDG_ORD_ORGNO"]
                return f"하한가 지정가 1주 접수: {o.body.get('msg1', '').strip()}"

            async def notice() -> str | tuple[str, str]:
                try:
                    msg = await asyncio.wait_for(ws.queue.get(), timeout=10)
                except asyncio.TimeoutError:
                    return "WARN", "10초 내 체결통보(접수) 없음"
                return f"통보 수신({len(msg.records[0])}필드, 복호화 {'O' if msg.encrypted else '-'})"

            async def cancel() -> str:
                c = await self.rest.request(tr.KR_ORDER_RVSECNCL, {
                    **self.rest.account_params(), "KRX_FWDG_ORD_ORGNO": state["orgno"], "ORGN_ODNO": state["odno"],
                    "ORD_DVSN": "00", "RVSE_CNCL_DVSN_CD": "02", "ORD_QTY": "0", "ORD_UNPR": "0",
                    "QTY_ALL_ORD_YN": "Y", "EXCG_ID_DVSN_CD": "KRX",
                })
                return f"취소 접수: {c.body.get('msg1', '').strip()}"

            await self.run("O1", "국내 지정가 주문", order)
            if "odno" in state:
                await self.run("O2", "주문 접수 통보", notice)
                await self.run("O3", "주문 취소", cancel)
        finally:
            await ws.close()


async def main_async(args: argparse.Namespace) -> int:
    settings = load_settings()
    setup_logging(settings)
    aliases = [args.account] if args.account else list(settings.accounts)
    unknown = [a for a in aliases if a not in settings.accounts]
    if unknown:
        print(f"알 수 없는 계좌: {unknown} (설정: {list(settings.accounts)})", file=sys.stderr)
        return 2

    all_results: list[Result] = []
    async with httpx.AsyncClient(timeout=15.0) as http:
        for alias in aliases:
            account = settings.accounts[alias]
            markets = [m for m in account.profile.markets if args.market in ("all", m.lower())]
            print(f"\n=== [{alias}] env={account.profile.env} markets={markets} ===")
            c = Checker(settings, account, http, args.ws_timeout)
            await c.common()
            if c.results and c.results[0].status == "FAIL":
                print("  토큰 발급 실패로 이후 항목 생략")
                all_results += c.results
                continue
            if "KR" in markets:
                await c.kr()
            if "US" in markets:
                await c.us()
            if not args.skip_rate:
                await c.rate_limit()
            await c.realtime(markets)
            if args.order_test and "KR" in markets:
                await c.kr_order_test()
            all_results += c.results

    counts = {s: sum(r.status == s for r in all_results) for s in ("OK", "WARN", "FAIL", "SKIP")}
    print(f"\n결과: " + " / ".join(f"{k} {v}" for k, v in counts.items()))

    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    out = settings.logs_dir / f"kis_check_{datetime.now(KST):%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps([asdict(r) for r in all_results], ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"상세: {out}")
    return 1 if counts["FAIL"] else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KIS 연결 점검")
    parser.add_argument("--account", help="계좌 alias (기본: 전체)")
    parser.add_argument("--market", choices=["all", "kr", "us"], default="all")
    parser.add_argument("--ws-timeout", type=float, default=15.0, help="실시간 데이터 대기 초")
    parser.add_argument("--skip-rate", action="store_true", help="호출 속도 점검 생략")
    parser.add_argument("--order-test", action="store_true", help="모의 계좌 주문/취소 점검 (paper 전용)")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(main_async(args))
    except SettingsError as e:
        print(f"[설정 오류] {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
