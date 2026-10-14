"""종목 카탈로그: 메모리 레지스트리(InstrumentRegistry) + DB(instruments) 영속화.

- 종목 마스터(central/marketdata/master.py)를 일괄 적재한다 (source=master). 관리자가 정한 nxt_tradable 은 유지
- 관리자 등록(PUT /admin/instruments)은 source=manual
- 국내 6자리 코드는 마스터에 없어도 자동 등록 (거래소 KRX, NXT 거래 가능 여부 모름 → NXT 경로 주문 불가)
- 미국은 주문에 거래소 코드(NASD/NYSE/AMEX)가 필요하므로 마스터나 관리자 등록에 있는 종목만 허용
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Callable, Iterable

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert

from central.core.instruments import Instrument, InstrumentRegistry
from central.core.market_hours import now_kst
from central.oms.errors import OmsError
from central.storage.db import Database
from central.storage.models import InstrumentRow

US_EXCHANGES = ("NASD", "NYSE", "AMEX")
KINDS = ("stock", "etf", "etn", "other")
_KR_CODE = re.compile(r"[0-9A-Z]{6}")
_US_CODE = re.compile(r"[A-Z][A-Z0-9.\-/]{0,15}")   # 예: BRK/B (마스터 표기)


def _inst(r: InstrumentRow) -> Instrument:
    return Instrument(r.id, r.market, r.code, r.exchange, r.name, r.nxt_tradable, r.kind)  # type: ignore[arg-type]


class InstrumentCatalog:
    def __init__(self, db: Database, registry: InstrumentRegistry, clock: Callable[[], datetime] = now_kst) -> None:
        self.db, self.registry, self.clock = db, registry, clock

    def load(self) -> int:
        with self.db.session() as s:
            rows = list(s.scalars(select(InstrumentRow)))
        for r in rows:
            self.registry.add(_inst(r))
        return len(rows)

    def upsert(self, market: str, code: str, exchange: str | None = None, name: str = "",
               nxt_tradable: bool = False, kind: str = "stock") -> Instrument:
        market, code = market.upper(), code.upper()
        exchange = self._validate(market, code, exchange)
        if market == "US":
            nxt_tradable = False
        if kind not in KINDS:
            raise OmsError("INVALID_REQUEST", f"kind 는 {KINDS} 중 하나")
        inst = Instrument(Instrument.make_id(market, code), market, code, exchange, name, nxt_tradable, kind)  # type: ignore[arg-type]
        with self.db.session() as s, s.begin():
            row = s.get(InstrumentRow, inst.id)
            if row is None:
                row = InstrumentRow(id=inst.id)
                s.add(row)
            row.market, row.code, row.exchange, row.name = market, code, exchange, name
            row.nxt_tradable, row.kind, row.source, row.updated_at = nxt_tradable, kind, "manual", self.clock()
            if row.name_en is None:
                row.name_en = ""
        self.registry.add(inst)
        return inst

    def bulk_load(self, items: Iterable, source: str = "master") -> int:
        """종목 마스터 일괄 적재 (items: MasterItem). 이미 있는 종목의 nxt_tradable 은 유지한다."""
        now = self.clock()
        rows = [{"id": Instrument.make_id(i.market, i.code), "market": i.market, "code": i.code,
                 "exchange": i.exchange, "name": i.name[:80], "name_en": i.name_en[:80], "kind": i.kind,
                 "nxt_tradable": False, "source": source, "updated_at": now}
                for i in items if self._valid_code(i.market, i.code)]
        with self.db.session() as s, s.begin():
            for chunk in range(0, len(rows), 500):
                stmt = insert(InstrumentRow).values(rows[chunk:chunk + 500])
                stmt = stmt.on_conflict_do_update(index_elements=["id"], set_={
                    "exchange": stmt.excluded.exchange, "name": stmt.excluded.name, "name_en": stmt.excluded.name_en,
                    "kind": stmt.excluded.kind, "source": stmt.excluded.source, "updated_at": stmt.excluded.updated_at})
                s.execute(stmt)
        self.load()
        return len(rows)

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
            raise OmsError("INVALID_SYMBOL", f"미등록 미국 종목 {symbol}: 종목 마스터 갱신 또는 관리자 등록 필요")
        raise OmsError("INVALID_SYMBOL", f"종목 형식은 KR:005930, US:AAPL: {symbol}")

    def search(self, q: str = "", market: str | None = None, limit: int = 50) -> list[dict]:
        q = q.strip()
        with self.db.session() as s:
            stmt = select(InstrumentRow)
            if market:
                stmt = stmt.where(InstrumentRow.market == market.upper())
            if q:
                like = f"%{q}%"
                stmt = stmt.where(InstrumentRow.code.ilike(like) | InstrumentRow.name.ilike(like)
                                  | InstrumentRow.name_en.ilike(like))
            rows = list(s.scalars(stmt.order_by(InstrumentRow.id).limit(limit * 5)))
        # 코드나 이름이 정확히 일치하는 종목, 그다음 앞부분이 일치하는 종목을 먼저
        qu = q.upper()
        rows.sort(key=lambda r: (r.code != qu and r.name.upper() != qu and r.name_en.upper() != qu,
                                 not (r.code.startswith(qu) or r.name.upper().startswith(qu)), r.id))
        return [{"id": r.id, "market": r.market, "code": r.code, "exchange": r.exchange, "name": r.name,
                 "name_en": r.name_en, "kind": r.kind, "nxt_tradable": r.nxt_tradable} for r in rows[:limit]]

    def has_master(self) -> bool:
        with self.db.session() as s:
            return s.scalar(select(InstrumentRow.id).where(InstrumentRow.source == "master").limit(1)) is not None

    def counts(self) -> dict[str, int]:
        with self.db.session() as s:
            q = select(InstrumentRow.market, func.count()).group_by(InstrumentRow.market)
            return {market: n for market, n in s.execute(q)}

    # ───────────── 검증 ─────────────
    @staticmethod
    def _valid_code(market: str, code: str) -> bool:
        return bool((_KR_CODE if market == "KR" else _US_CODE).fullmatch(code))

    def _validate(self, market: str, code: str, exchange: str | None) -> str:
        if market == "KR":
            if not _KR_CODE.fullmatch(code):
                raise OmsError("INVALID_SYMBOL", f"국내 종목코드는 6자리: {code}")
            return "KRX"
        if market == "US":
            if not _US_CODE.fullmatch(code):
                raise OmsError("INVALID_SYMBOL", f"미국 종목코드 형식 오류: {code}")
            if exchange not in US_EXCHANGES:
                raise OmsError("INVALID_REQUEST", f"미국 종목은 exchange 가 {US_EXCHANGES} 중 하나여야 함")
            return exchange  # type: ignore[return-value]
        raise OmsError("MARKET_NOT_SUPPORTED", market)
