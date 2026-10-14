"""서브계좌 API 키 발급, 폐기, 인증.

키 형식: "cs_" + 43자 URL-safe 무작위 문자열 (256비트). DB 에는 SHA-256 해시만 저장하고
평문은 발급 응답에서 한 번만 돌려준다. 엔트로피가 충분하므로 솔트 없는 해시로 조회한다.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy import select

from central.core.events import EventBus
from central.core.market_hours import now_kst
from central.oms.accounts import SubAccount, _view, record_event, tx
from central.oms.errors import OmsError
from central.storage.db import Database
from central.storage.models import ApiKeyRow, SubAccountRow

KEY_PREFIX = "cs_"
_PREFIX_LEN = 9  # 표시용: "cs_" + 6자
_TOUCH_INTERVAL = timedelta(minutes=1)


@dataclass(frozen=True)
class ApiKeyInfo:
    id: int
    sub_account_id: int
    prefix: str
    label: str | None
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _info(r: ApiKeyRow) -> ApiKeyInfo:
    return ApiKeyInfo(r.id, r.sub_account_id, r.prefix, r.label, r.created_at, r.last_used_at, r.revoked_at)


class ApiKeyService:
    def __init__(self, db: Database, bus: EventBus, clock: Callable[[], datetime] = now_kst) -> None:
        self.db, self.bus, self.clock = db, bus, clock

    def issue(self, sub_account_id: int, label: str | None = None) -> tuple[ApiKeyInfo, str]:
        key = KEY_PREFIX + secrets.token_urlsafe(32)
        with tx(self.db, self.bus) as (s, ev):
            sub = s.get(SubAccountRow, sub_account_id)
            if sub is None:
                raise OmsError("NOT_FOUND", f"서브계좌 {sub_account_id} 없음")
            if sub.type == "system":
                raise OmsError("INVALID_REQUEST", "시스템 서브계좌(미배정)에는 API 키를 발급하지 않음")
            if sub.status == "closed":
                raise OmsError("INVALID_REQUEST", "해지된 서브계좌")
            row = ApiKeyRow(sub_account_id=sub_account_id, key_hash=hash_key(key), prefix=key[:_PREFIX_LEN],
                            label=label, created_at=self.clock())
            s.add(row)
            s.flush()
            record_event(s, ev, "API_KEY_ISSUED", sub_account_id, {"key_id": row.id, "prefix": row.prefix}, self.clock())
            return _info(row), key

    def revoke(self, key_id: int) -> ApiKeyInfo:
        with tx(self.db, self.bus) as (s, ev):
            row = s.get(ApiKeyRow, key_id)
            if row is None:
                raise OmsError("NOT_FOUND", f"API 키 {key_id} 없음")
            if row.revoked_at is None:
                row.revoked_at = self.clock()
                record_event(s, ev, "API_KEY_REVOKED", row.sub_account_id, {"key_id": row.id}, self.clock())
            return _info(row)

    def list(self, sub_account_id: int | None = None) -> list[ApiKeyInfo]:
        with self.db.session() as s:
            q = select(ApiKeyRow).order_by(ApiKeyRow.id)
            if sub_account_id is not None:
                q = q.where(ApiKeyRow.sub_account_id == sub_account_id)
            return [_info(r) for r in s.scalars(q)]

    def authenticate(self, key: str | None) -> SubAccount | None:
        """유효한 키면 서브계좌를 돌려준다. 폐기된 키, 해지된 서브계좌는 None."""
        if not key or not key.startswith(KEY_PREFIX) or len(key) > 128:
            return None
        with self.db.session() as s, s.begin():
            row = s.scalar(select(ApiKeyRow).where(ApiKeyRow.key_hash == hash_key(key)))
            if row is None or row.revoked_at is not None:
                return None
            sub = s.get(SubAccountRow, row.sub_account_id)
            if sub is None or sub.status == "closed":
                return None
            now = self.clock()
            last = row.last_used_at
            if last is None or now.replace(tzinfo=None) - last.replace(tzinfo=None) > _TOUCH_INTERVAL:
                row.last_used_at = now
            return _view(sub)
