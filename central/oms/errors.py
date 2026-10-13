"""OMS 오류. code 는 API 에러 코드(docs/PLAN.md 8.6)와 같다."""

from __future__ import annotations


class OmsError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
