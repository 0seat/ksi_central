"""python -m central history ... : 과거 시세 보관소 수동 실행 (가져오기, 조회, 이벤트 확인).

  history fetch KR:005930 US:AAPL INDEX:KR:0001 INDEX:US:COMP   1분봉과 일봉 (처음이면 KIS 가 주는 끝까지)
  history fetch --top-kr 10 --top-us 10                         코스피/나스닥 시가총액 상위
  history fetch KR:005930 --only daily --full                   일봉과 이벤트를 처음부터 다시
  history show US:AAPL --interval 1d --adjust split --tail 5
  history events US:AAPL
  history list
"""

from __future__ import annotations

import argparse
import asyncio
import time
from datetime import datetime

import httpx

from central.broker.kis import tr
from central.broker.kis.broker import KisBroker, _rows
from central.core.instruments import Instrument, InstrumentRegistry
from central.core.market_hours import KST
from central.marketdata.history import ADJUST_MODES, HistoryFetcher, HistoryStore, SeriesKey
from central.settings import Settings, load_settings


def history_root(settings: Settings):
    return settings.data_dir / "history"


def _account(settings: Settings, market: str) -> str:
    """시세 계좌 설정(market_data.kr_account/us_account)을 따르고, 없으면 그 시장의 실전 계좌. 지수는 실전 계좌."""
    md = settings.market_data
    chosen = {"KR": md.kr_account, "US": md.us_account}.get(market)
    if chosen:
        return chosen
    want = "KR" if market == "INDEX" else market
    for alias, acc in sorted(settings.accounts.items(), key=lambda x: x[1].profile.env != "real"):
        if want in acc.profile.markets:
            return alias
    raise SystemExit(f"{market} 을 받을 계좌 없음")


def _instruments(settings: Settings) -> InstrumentRegistry:
    from central.oms.catalog import InstrumentCatalog
    from central.storage.db import Database

    reg = InstrumentRegistry()
    InstrumentCatalog(Database.file(settings.data_dir / "central.db"), reg).load()
    return reg


async def _top(broker: KisBroker, market: str, n: int) -> list[str]:
    if market == "KR":
        body = (await broker.rest.request(tr.KR_MARKET_CAP, {
            "fid_input_price_2": "", "fid_cond_mrkt_div_code": "J", "fid_cond_scr_div_code": "20174",
            "fid_div_cls_code": "1", "fid_input_iscd": "0001", "fid_trgt_cls_code": "0",
            "fid_trgt_exls_cls_code": "0", "fid_input_price_1": "", "fid_vol_cnt": ""})).body
        return [f"KR:{x['mksc_shrn_iscd']}" for x in _rows(body, "output")][:n]
    body = (await broker.rest.request(tr.US_MARKET_CAP, {
        "EXCD": "NAS", "VOL_RANG": "0", "CURR_GB": "0", "KEYB": "", "AUTH": ""})).body
    return [f"US:{x['symb']}" for x in _rows(body, "output2")][:n]


async def _fetch(args: argparse.Namespace, settings: Settings) -> int:
    store = HistoryStore(history_root(settings))
    async with httpx.AsyncClient(timeout=30) as http:
        brokers: dict[str, KisBroker] = {}

        def broker_for(market: str) -> KisBroker:
            alias = _account(settings, market)
            if alias not in brokers:
                brokers[alias] = KisBroker(settings.accounts[alias], http, settings.tokens_dir)
            return brokers[alias]

        symbols = list(args.symbols)
        if args.top_kr:
            symbols += await _top(broker_for("INDEX"), "KR", args.top_kr)
        if args.top_us:
            symbols += await _top(broker_for("INDEX"), "US", args.top_us)
        reg = _instruments(settings) if any(not s.upper().startswith("INDEX:") for s in symbols) else None
        fetcher = HistoryFetcher(store, broker_for)
        print(f"대상 {len(symbols)}개: {' '.join(symbols)}", flush=True)
        failed = 0
        for sym in dict.fromkeys(symbols):
            key = SeriesKey.parse(sym)
            t0 = time.monotonic()
            try:
                if key.index:
                    if args.only in (None, "daily"):
                        print(" ", await fetcher.index_daily(key, full=args.full), flush=True)
                    if args.only in (None, "minutes"):
                        print(" ", await fetcher.index_minutes(key), flush=True)
                    continue
                assert reg is not None
                inst = reg.get(str(key)) or Instrument(str(key), key.market, key.code,
                                                        "KRX" if key.market == "KR" else "NASD", key.code)
                # 일봉(이벤트)을 먼저: 분봉을 원주가로 되돌리는 데 쓴다
                if args.only in (None, "daily"):
                    print(" ", await fetcher.daily(inst, full=args.full), flush=True)
                if args.only in (None, "minutes"):
                    print(" ", await fetcher.minutes(inst, progress=lambda m: print("   ", m, flush=True)), flush=True)
            except Exception as e:   # 한 종목 실패가 나머지를 막지 않게
                failed += 1
                print(f"  [실패] {key}: {type(e).__name__}: {e}", flush=True)
            finally:
                print(f"  {key} {time.monotonic() - t0:.0f}초", flush=True)
    return 1 if failed else 0


def _show(args: argparse.Namespace, settings: Settings) -> int:
    store = HistoryStore(history_root(settings))
    key = SeriesKey.parse(args.symbol)
    # 날짜는 그 시장의 현지 날짜로 해석한다
    start = datetime.fromisoformat(args.start).replace(tzinfo=key.zone) if args.start else None
    end = datetime.fromisoformat(args.end).replace(tzinfo=key.zone, hour=23, minute=59, second=59) if args.end else None
    bars = store.read(key, args.interval, start, end, args.adjust)
    for b in bars[-args.tail:]:
        # 일봉 ts 는 거래일 0시 KST, 분봉은 현지 시각으로 보인다
        when = f"{b.ts.astimezone(KST):%Y-%m-%d}" if args.interval == "1d" else f"{b.ts.astimezone(key.zone):%Y-%m-%d %H:%M}"
        print(f"{when} O {b.open} H {b.high} L {b.low} C {b.close} V {b.volume}")
    print(f"({len(bars)}개 중 마지막 {min(args.tail, len(bars))}개, adjust={args.adjust})")
    return 0


def _events(args: argparse.Namespace, settings: Settings) -> int:
    store = HistoryStore(history_root(settings))
    for e in store.events(SeriesKey.parse(args.symbol)):
        print(f"{e.date}  x {e.ratio}  {e.kind}")
    return 0


def _repair(args: argparse.Namespace, settings: Settings) -> int:
    fetcher = HistoryFetcher(HistoryStore(history_root(settings)), lambda m: None)
    for sym in args.symbols or fetcher.store.symbols():
        key = SeriesKey.parse(sym)
        if not key.index:
            print(fetcher.repair_minutes(key))
    return 0


def _list(_: argparse.Namespace, settings: Settings) -> int:
    store = HistoryStore(history_root(settings))
    for sym in store.symbols():
        key = SeriesKey.parse(sym)
        days = store.daily(key)
        last = store.last_minute(key)
        n_ev = len(store.events(key))
        print(f"{sym:<16} 일봉 {len(days):>6}개 {days[0].ts.date() if days else '-'}~{days[-1].ts.date() if days else '-'}"
              f"  분봉 마지막 {last.astimezone(key.zone):%Y-%m-%d %H:%M}" if last else f"{sym:<16} 일봉 {len(days)}개",
              f" 이벤트 {n_ev}")
    return 0


def add_parser(sub) -> None:
    h = sub.add_parser("history", help="과거 시세 보관소 (원주가 + 기업행위 이벤트)")
    hs = h.add_subparsers(dest="action", required=True)
    f = hs.add_parser("fetch", help="KIS 에서 받아 보관 (처음이면 KIS 가 주는 끝까지)")
    f.add_argument("symbols", nargs="*", help="KR:005930 US:AAPL INDEX:KR:0001 INDEX:US:COMP")
    f.add_argument("--top-kr", type=int, default=0, help="코스피 시가총액 상위 N")
    f.add_argument("--top-us", type=int, default=0, help="나스닥 시가총액 상위 N")
    f.add_argument("--only", choices=["minutes", "daily"])
    f.add_argument("--full", action="store_true", help="일봉과 이벤트를 처음부터 다시")
    s = hs.add_parser("show", help="보관본 조회")
    s.add_argument("symbol")
    s.add_argument("--interval", choices=["1m", "1d"], default="1d")
    s.add_argument("--adjust", choices=ADJUST_MODES, default="none")
    s.add_argument("--start")
    s.add_argument("--end")
    s.add_argument("--tail", type=int, default=10)
    e = hs.add_parser("events", help="기업행위 이벤트 (분할/병합/배당)")
    e.add_argument("symbol")
    hs.add_parser("list", help="보관 중인 종목")
    rp = hs.add_parser("repair", help="수정주가로 들어간 분봉을 원주가로 되돌림 (일봉/이벤트 기준, 반복 실행 안전)")
    rp.add_argument("symbols", nargs="*")
    h.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    settings = load_settings()
    if args.action == "fetch":
        if not args.symbols and not args.top_kr and not args.top_us:
            raise SystemExit("종목을 주거나 --top-kr/--top-us 를 쓰세요")
        return asyncio.run(_fetch(args, settings))
    return {"show": _show, "events": _events, "list": _list, "repair": _repair}[args.action](args, settings)
