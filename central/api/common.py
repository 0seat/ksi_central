"""API 공통: 응답 형식, 직렬화, 오류, 인증 (docs/PLAN.md 8.1, 8.6).

응답: {"ok": true, "data": ...} / {"ok": false, "error": {"code", "message"}}
금액은 문자열 Decimal, 시각은 ISO8601 KST.
"""

from __future__ import annotations

import dataclasses
import hmac
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BeforeValidator

from central.app import CentralApp
from central.core.market_hours import KST
from central.oms.accounts import SubAccount
from central.oms.errors import OmsError

STATUS_BY_CODE = {
    "UNAUTHORIZED": 401,
    "FORBIDDEN": 403,
    "NOT_FOUND": 404,
    "ORDER_NOT_FOUND": 404,
    "DUPLICATE": 409,
    "DUPLICATE_CLIENT_ORDER_ID": 409,
    "ALREADY_BOOTSTRAPPED": 409,
    "RATE_LIMITED": 429,
    "BROKER_UNAVAILABLE": 503,
    "NOT_IMPLEMENTED": 501,
}


def out(obj: Any) -> Any:
    """JSON 으로 보낼 수 있는 형태로 변환. Decimal 은 문자열, 시간대 없는 시각은 KST 로 간주."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: out(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return (obj.replace(tzinfo=KST) if obj.tzinfo is None else obj.astimezone(KST)).isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): out(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [out(v) for v in obj]
    return obj


def ok(data: Any = None) -> dict:
    return {"ok": True, "data": out(data)}


def error_response(code: str, message: str, status: int | None = None) -> JSONResponse:
    return JSONResponse({"ok": False, "error": {"code": code, "message": message}},
                        status_code=status or STATUS_BY_CODE.get(code, 400))


class ApiError(OmsError):
    """API 계층 전용 오류 (인증 등). OmsError 와 같은 방식으로 응답한다."""


def _no_float(v: Any) -> Any:
    # JSON 숫자(float)는 이진 근사가 섞이므로 금액은 문자열이나 정수로만 받는다
    if isinstance(v, float):
        raise ValueError("금액과 가격은 문자열로 보낼 것 (예: \"187.25\")")
    return v


Money = Annotated[Decimal, BeforeValidator(_no_float)]


# ───────────── 의존성 ─────────────
def central(request: Request) -> CentralApp:
    return request.app.state.central


def require_admin(request: Request, x_admin_key: Annotated[str | None, Header()] = None) -> None:
    expected = request.app.state.central.settings.admin_key
    if expected is None:
        raise ApiError("FORBIDDEN", "관리자 키가 설정되지 않아 관리자 API 를 쓸 수 없음 (secrets.toml [central] admin_key)")
    if not x_admin_key or not hmac.compare_digest(x_admin_key.encode(), expected.get_secret_value().encode()):
        raise ApiError("UNAUTHORIZED", "관리자 키가 올바르지 않음")


def require_sub(request: Request, x_api_key: Annotated[str | None, Header()] = None) -> SubAccount:
    sub = request.app.state.central.apikeys.authenticate(x_api_key)
    if sub is None:
        raise ApiError("UNAUTHORIZED", "API 키가 없거나 올바르지 않음")
    return sub


App = Annotated[CentralApp, Depends(central)]
Sub = Annotated[SubAccount, Depends(require_sub)]
