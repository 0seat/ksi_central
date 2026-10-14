"""KIS 접근토큰 / WebSocket 접속키 관리.

- 접근토큰은 1일 1회 발급이 원칙이고, 짧은 시간 재발급은 1분 1회로 제한된다.
  → 토큰을 CENTRAL_HOME/state/tokens/<alias>.json 에 캐시하고 만료 임박 시에만 재발급한다.
- WS 접속키(approval_key)는 디스크에 저장하지 않는다.
- 발급된 값은 즉시 로그 마스커에 등록한다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from central.broker.kis.tr import REST_BASE
from central.core.logging import masker
from central.settings import Account

log = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")
REFRESH_MARGIN = timedelta(minutes=30)   # 만료 30분 전부터는 재발급
REISSUE_MIN_INTERVAL = timedelta(seconds=61)


class KisAuthError(Exception):
    pass


@dataclass(frozen=True)
class Token:
    access_token: str
    expires_at: datetime
    issued_at: datetime

    def usable(self, now: datetime) -> bool:
        return self.expires_at - now > REFRESH_MARGIN

    def to_json(self) -> dict:
        return {
            "access_token": self.access_token,
            "expires_at": self.expires_at.isoformat(),
            "issued_at": self.issued_at.isoformat(),
        }

    @classmethod
    def from_json(cls, d: dict) -> Token:
        return cls(d["access_token"], datetime.fromisoformat(d["expires_at"]), datetime.fromisoformat(d["issued_at"]))


class TokenCache:
    def __init__(self, tokens_dir: Path, alias: str) -> None:
        self.path = tokens_dir / f"{alias}.json"

    def load(self) -> Token | None:
        try:
            return Token.from_json(json.loads(self.path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return None
        except (ValueError, KeyError):
            log.warning("토큰 캐시 손상, 무시: %s", self.path)
            return None

    def save(self, token: Token) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(token.to_json()), encoding="utf-8")
        os.replace(tmp, self.path)


def _parse_expiry(body: dict, issued_at: datetime) -> datetime:
    raw = body.get("access_token_token_expired")
    if raw:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
    return issued_at + timedelta(seconds=int(body.get("expires_in", 86400)))


class KisAuth:
    def __init__(self, account: Account, tokens_dir: Path, http: httpx.AsyncClient) -> None:
        self.account = account
        self._base = REST_BASE[account.profile.env]
        self._cache = TokenCache(tokens_dir, account.alias)
        self._http = http
        self._lock = asyncio.Lock()
        self._token: Token | None = None
        self.last_issue_was_new = False

    @property
    def app_key(self) -> str:
        return self.account.secrets.app_key.get_secret_value()

    @property
    def app_secret(self) -> str:
        return self.account.secrets.app_secret.get_secret_value()

    @property
    def token_expires_at(self) -> datetime | None:
        """메모리에 있는 토큰의 만료 시각 (상태 조회용, 발급하지 않음)."""
        return self._token.expires_at if self._token else None

    def cached_token(self) -> Token | None:
        token = self._cache.load()
        if token:
            masker.add([token.access_token])
        return token

    async def access_token(self) -> str:
        async with self._lock:
            now = datetime.now(KST)
            if self._token is None:
                self._token = self.cached_token()
            if self._token and self._token.usable(now):
                self.last_issue_was_new = False
                return self._token.access_token
            self._token = await self._issue(previous=self._token)
            self.last_issue_was_new = True
            return self._token.access_token

    async def invalidate(self) -> None:
        """서버가 토큰 만료를 알린 경우 호출. 다음 access_token() 에서 재발급."""
        async with self._lock:
            if self._token:
                self._token = Token(self._token.access_token, datetime.now(KST), self._token.issued_at)

    async def _issue(self, previous: Token | None) -> Token:
        if previous is not None:
            wait = (previous.issued_at + REISSUE_MIN_INTERVAL - datetime.now(KST)).total_seconds()
            if wait > 0:
                log.info("[%s] 토큰 재발급 1분 제한 대기 %.0f초", self.account.alias, wait)
                await asyncio.sleep(wait)

        issued_at = datetime.now(KST)
        res = await self._http.post(
            f"{self._base}/oauth2/tokenP",
            json={"grant_type": "client_credentials", "appkey": self.app_key, "appsecret": self.app_secret},
        )
        body = _json(res)
        token_value = body.get("access_token")
        if res.status_code != 200 or not token_value:
            raise KisAuthError(f"[{self.account.alias}] 토큰 발급 실패 HTTP {res.status_code}: {_err(body)}")
        masker.add([token_value])
        token = Token(token_value, _parse_expiry(body, issued_at), issued_at)
        self._cache.save(token)
        log.info("[%s] 접근토큰 발급, 만료 %s", self.account.alias, token.expires_at.isoformat())
        return token

    async def approval_key(self) -> str:
        res = await self._http.post(
            f"{self._base}/oauth2/Approval",
            json={"grant_type": "client_credentials", "appkey": self.app_key, "secretkey": self.app_secret},
        )
        body = _json(res)
        key = body.get("approval_key")
        if res.status_code != 200 or not key:
            raise KisAuthError(f"[{self.account.alias}] WS 접속키 발급 실패 HTTP {res.status_code}: {_err(body)}")
        masker.add([key])
        return key


def _json(res: httpx.Response) -> dict:
    try:
        data = res.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _err(body: dict) -> str:
    return f"{body.get('error_code') or body.get('msg_cd', '')} {body.get('error_description') or body.get('msg1', '')}".strip()
