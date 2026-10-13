"""중앙증권 설정 로더.

설정과 비밀정보는 저장소 밖 ``CENTRAL_HOME`` 에서만 읽는다.
저장소 안의 ``.env`` 나 비밀 파일은 읽지 않는다 (docs/PLAN.md 11장).

    %CENTRAL_HOME%/
      config/central.toml      일반 설정 (실계좌 profile 등)
      secrets/secrets.toml     앱키, 계좌번호 등 비밀정보
      state/tokens/            토큰 캐시
      data/  logs/  backups/
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator, model_validator

REPO_ROOT = Path(__file__).resolve().parent.parent
HOME_ENV_VAR = "CENTRAL_HOME"

Env = Literal["real", "paper"]
Market = Literal["KR", "US"]
KrRoute = Literal["KRX", "NXT", "SOR"]


class SettingsError(Exception):
    """설정 파일이 없거나 잘못되었을 때."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AccountProfile(_Model):
    """실계좌 profile (비밀 아님). 모의/실전 차이는 여기에만 둔다."""

    env: Env
    markets: list[Market] = Field(default_factory=lambda: ["KR"])
    kr_routes: list[KrRoute] = Field(default_factory=lambda: ["KRX"])
    kr_default_route: KrRoute = "KRX"
    integrated_margin: bool = False
    rate_limit_per_sec: float = Field(default=2.0, gt=0)
    ws_max_subscriptions: int = Field(default=40, ge=1)
    trading_enabled: bool = False
    # 미배정 서브계좌의 USD 재고(환전 풀). 최소값 아래로 내려가면 알림 (M6)
    usd_inventory_target: Decimal | None = None
    usd_inventory_min: Decimal | None = None

    @model_validator(mode="after")
    def _default_route_supported(self) -> AccountProfile:
        if self.kr_default_route not in self.kr_routes:
            raise ValueError(f"kr_default_route={self.kr_default_route} 가 kr_routes={self.kr_routes} 에 없음")
        return self


class AccountSecrets(_Model):
    app_key: SecretStr
    app_secret: SecretStr
    hts_id: SecretStr
    account_no: SecretStr
    product_code: str = "01"

    @field_validator("app_key", "app_secret", "hts_id")
    @classmethod
    def _not_empty(cls, v: SecretStr) -> SecretStr:
        if not v.get_secret_value().strip():
            raise ValueError("값이 비어 있음")
        return v

    @field_validator("account_no")
    @classmethod
    def _account_no_8_digits(cls, v: SecretStr) -> SecretStr:
        if not re.fullmatch(r"\d{8}", v.get_secret_value()):
            raise ValueError("계좌번호 앞 8자리 숫자여야 함")
        return v

    @field_validator("product_code")
    @classmethod
    def _product_code_2_digits(cls, v: str) -> str:
        if not re.fullmatch(r"\d{2}", v):
            raise ValueError("계좌상품코드는 2자리 숫자여야 함")
        return v


class Account(_Model):
    alias: str
    profile: AccountProfile
    secrets: AccountSecrets


class ServerConfig(_Model):
    hosts: list[str] = Field(default_factory=lambda: ["127.0.0.1"])
    port: int = Field(default=8700, ge=1, le=65535)

    @field_validator("hosts")
    @classmethod
    def _no_wildcard_bind(cls, v: list[str]) -> list[str]:
        if any(h in ("0.0.0.0", "::") for h in v):
            raise ValueError("전체 인터페이스 바인딩 금지: Tailscale IP 또는 127.0.0.1 만 사용")
        return v


class LoggingConfig(_Model):
    level: str = "INFO"
    retention_days: int = Field(default=30, ge=1)


class Settings(_Model):
    home: Path
    server: ServerConfig = Field(default_factory=ServerConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    accounts: dict[str, Account]
    admin_key: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: SecretStr | None = None

    @property
    def config_dir(self) -> Path:
        return self.home / "config"

    @property
    def secrets_dir(self) -> Path:
        return self.home / "secrets"

    @property
    def state_dir(self) -> Path:
        return self.home / "state"

    @property
    def tokens_dir(self) -> Path:
        return self.state_dir / "tokens"

    @property
    def data_dir(self) -> Path:
        return self.home / "data"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def backups_dir(self) -> Path:
        return self.home / "backups"

    def secret_values(self) -> list[str]:
        """로그 마스킹에 등록할 비밀 문자열 목록."""
        values: list[str] = []
        for opt in (self.admin_key, self.telegram_bot_token, self.telegram_chat_id):
            if opt is not None:
                values.append(opt.get_secret_value())
        for acc in self.accounts.values():
            s = acc.secrets
            acct = s.account_no.get_secret_value()
            values += [
                s.app_key.get_secret_value(),
                s.app_secret.get_secret_value(),
                s.hts_id.get_secret_value(),
                f"{acct}-{s.product_code}",
                f"{acct}{s.product_code}",
                acct,
            ]
        return [v for v in values if v]


def default_home() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "CentralSecurities"
    return Path.home() / ".central"


def resolve_home(explicit: Path | str | None = None) -> Path:
    """CENTRAL_HOME 결정. 저장소 내부 경로는 거부한다."""
    raw = explicit or os.environ.get(HOME_ENV_VAR) or default_home()
    home = Path(raw).expanduser().resolve()
    if home == REPO_ROOT or home.is_relative_to(REPO_ROOT):
        raise SettingsError(f"CENTRAL_HOME({home})이 저장소 안에 있음. 비밀정보 유출 방지를 위해 저장소 밖 경로를 사용할 것")
    return home


def _read_toml(path: Path, hint: str) -> dict:
    if not path.is_file():
        raise SettingsError(f"설정 파일 없음: {path}\n  → {hint}")
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise SettingsError(f"TOML 형식 오류: {path}: {e}") from e


def _opt_secret(value: str | None) -> SecretStr | None:
    return SecretStr(value) if value else None


def load_settings(home: Path | str | None = None) -> Settings:
    home_path = resolve_home(home)
    init_hint = "`python -m tools.init_home` 실행 후 값을 채울 것"
    config = _read_toml(home_path / "config" / "central.toml", init_hint)
    secrets = _read_toml(home_path / "secrets" / "secrets.toml", init_hint)

    profiles: dict = config.get("accounts", {})
    account_secrets: dict = secrets.get("accounts", {})
    if not profiles:
        raise SettingsError("central.toml 에 [accounts.<alias>] 가 하나도 없음")
    if set(profiles) != set(account_secrets):
        raise SettingsError(
            f"계좌 alias 불일치: central.toml={sorted(profiles)} / secrets.toml={sorted(account_secrets)}"
        )

    accounts: dict[str, Account] = {}
    for alias in profiles:
        try:
            accounts[alias] = Account(
                alias=alias,
                profile=AccountProfile(**profiles[alias]),
                secrets=AccountSecrets(**account_secrets[alias]),
            )
        except ValidationError as e:
            raise SettingsError(f"계좌 '{alias}' 설정 오류: {_describe(e)}") from None

    try:
        return Settings(
            home=home_path,
            server=ServerConfig(**config.get("server", {})),
            logging=LoggingConfig(**config.get("logging", {})),
            accounts=accounts,
            admin_key=_opt_secret(secrets.get("central", {}).get("admin_key")),
            telegram_bot_token=_opt_secret(secrets.get("telegram", {}).get("bot_token")),
            telegram_chat_id=_opt_secret(secrets.get("telegram", {}).get("chat_id")),
        )
    except ValidationError as e:
        raise SettingsError(f"설정 값 오류: {_describe(e)}") from None


def _describe(e: ValidationError) -> str:
    # pydantic 기본 메시지에는 입력값이 포함될 수 있으므로 위치와 사유만 남긴다.
    return "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
