"""종목 카탈로그: 메모리 레지스트리(InstrumentRegistry) + DB(instruments) 영속화.

M5 종목 마스터 전까지의 규칙
- 국내: 6자리 코드면 자동 등록 (거래소 KRX, NXT 거래 가능 여부는 모름 → False, NXT 경로 주문 불가)
- 미국: 주문에 거래소 코드(NASD/NYSE/AMEX)가 필요하므로 관리자가 등록한 종목만 허용
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Callable

from sqlalchemy import select

from central.core.instruments import Instrument, InstrumentRegistry
from central.core.market_hours import now_kst
from central.oms.errors import OmsError
from central.storage.db import Database
from central.storage.models import InstrumentRow

US_EXCHANGES = ("NASD", "NYSE", "AMEX")
_KR_CODE = re.compile(r"[0-9A-Z]{6}")
_US_CODE = re.compile(r"[A-Z][A-Z0-9.\-]{0,11}")


class InstrumentCatalog:
    def __init__(self, db: Database, registry: InstrumentRegistry, clock: Callable[[], datetime] = now_kst) -> None:
        self.db, self.registry, self.clock = db, registry, clock

    def load(self) -> int:
        with self.db.session() as s:
            rows = list(s.scalars(select(InstrumentRow)))
        for r in rows:
            self.registry.add(Instrument(r.id, r.market, r.code, r.exchange, r.name, r.nxt_tradable))  # type: ignore[arg-type]
        return len(rows)

    def upsert(self, market: str, code: str, exchange: str | None = None, name: str = "",
               nxt_tradable: bool = False) -> Instrument:
        market, code = market.upper(), code.upper()
        if market == "KR":
            if not _KR_CODE.fullmatch(code):
                raise OmsError("INVALID_SYMBOL", f"국내 종목코드는 6자리: {code}")
            exchange = "KRX"
        elif market == "US":
            if not _US_CODE.fullmatch(code):
                raise OmsError("INVALID_SYMBOL", f"미국 종목코드 형식 오류: {code}")
            if exchange not in US_EXCHANGES:
                raise OmsError("INVALID_REQUEST", f"미국 종목은 exchange 가 {US_EXCHANGES} 중 하나여야 함")
            nxt_tradable = False
        else:
            raise OmsError("MARKET_NOT_SUPPORTED", market)
        inst = Instrument(Instrument.make_id(market, code), market, code, exchange, name, nxt_tradable)  # type: ignore[arg-type]
        with self.db.session() as s, s.begin():
            row = s.get(InstrumentRow, inst.id)
            if row is None:
                row = InstrumentRow(id=inst.id)
                s.add(row)
            row.market, row.code, row.exchange, row.name = market, code, exchange, name
            row.nxt_tradable, row.updated_at = nxt_tradable, self.clock()
        self.registry.add(inst)
        return inst

    def resolve(self, symbol: str) -> Instrument:
        """주문/시세 요청의 종목을 찾는다. 국내 미등록 종목은 자동 등록."""
        symbol = symbol.strip().upper()
        found = self.registry.get(symbol)
        if found is not None:
            return found
        market, _, code = symbol.partition(":")
        if market == "KR":
            return self.upsert("KR", code)
        if market == "US":
            raise OmsError("INVALID_SYMBOL", f"미등록 미국 종목 {symbol}: 관리자가 거래소와 함께 등록해야 함")
        raise OmsError("INVALID_SYMBOL", f"종목 형식은 KR:005930, US:AAPL: {symbol}")

    def search(self, q: str = "", market: str | None = None, limit: int = 50) -> list[Instrument]:
        q = q.strip().upper()
        with self.db.session() as s:
            stmt = select(InstrumentRow).order_by(InstrumentRow.id)
            if market:
                stmt = stmt.where(InstrumentRow.market == market.upper())
            rows = list(s.scalars(stmt))
        hits = [r for r in rows if not q or q in r.code or q in r.name.upper()]
        return [Instrument(r.id, r.market, r.code, r.exchange, r.name, r.nxt_tradable) for r in hits[:limit]]  # type: ignore[arg-type]
