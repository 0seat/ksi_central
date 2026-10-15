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

import ipaddress
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
# Tailscale 주소 대역 (IPv4 CGNAT, IPv6 ULA)
_TAILSCALE_NETS = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))


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
    ws_max_subscriptions: int = Field(default=41, ge=1)  # 공식: 앱키당 실시간 등록 41건
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
    def _loopback_or_tailscale_only(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("바인딩 주소가 하나 이상 필요")
        for h in v:
            try:
                ip = ipaddress.ip_address(h)
            except ValueError:
                raise ValueError(f"IP 주소만 사용 (호스트 이름 불가): {h}") from None
            if not (ip.is_loopback or any(ip in net for net in _TAILSCALE_NETS if ip.version == net.version)):
                raise ValueError(f"전체 인터페이스나 외부 주소 바인딩 금지: {h} (127.0.0.1 또는 Tailscale IP 만 사용)")
        return v


class MarketDataConfig(_Model):
    """시세 수신 설정. 시세는 계좌와 무관하므로 시장마다 실시간 등록 슬롯을 쓸 계좌 하나를 정한다.

    비워 두면 해당 시장을 지원하는 실전 계좌 우선. 실전 앱키의 등록 한도가 작을 때는
    국내 시세를 모의 계좌로 받을 수 있다 (모의는 KRX 시세만, 통합/NXT 시세 없음).
    """

    kr_account: str | None = None
    us_account: str | None = None
    watchlist: list[str] = Field(default_factory=list)   # 클라이언트 구독과 무관하게 상시 수집할 종목
    store_ticks: bool = True
    flush_seconds: float = Field(default=30.0, gt=0)


class LoggingConfig(_Model):
    level: str = "INFO"
    retention_days: int = Field(default=30, ge=1)


class ProgramConfig(_Model):
    """감시자(supervise)가 서버와 함께 띄우는 프로그램. 저장소 루트에서 같은 파이썬으로 실행한다."""
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    args: list[str] = Field(min_length=1)   # 예: ["-m", "apps.pilot", "run"]


class OpsConfig(_Model):
    """운영: 알림, 일정 작업, 감시 (docs/PLAN.md M6)."""
    notify_min_level: Literal["info", "warning", "error"] = "info"
    alert_cooldown_minutes: float = Field(default=15, ge=0)   # 같은 알림 반복 억제
    halt_on_reconcile_mismatch: bool = True
    backup_keep: int = Field(default=14, ge=1)
    disk_min_free_gb: float = Field(default=20, ge=0)
    ws_down_alert_seconds: int = Field(default=120, ge=10)
    stale_tick_minutes: float = Field(default=5, gt=0)        # 장중 상시 수집 종목의 체결이 이만큼 끊기면 알림
    programs: list[ProgramConfig] = Field(default_factory=list)

    @field_validator("programs")
    @classmethod
    def _unique_names(cls, v: list[ProgramConfig]) -> list[ProgramConfig]:
        names = [p.name for p in v]
        if len(names) != len(set(names)) or "serve" in names:
            raise ValueError("프로그램 이름이 중복되거나 예약어(serve)")
        return v


class HistoryConfig(_Model):
    """과거 시세 보관 (central/marketdata/history.py). 대상은 여기 목록 + 나스닥100 + 앱이 API 로 등록한 것
    (central/marketdata/history_targets.py). 처음 보는 대상은 서버가 백그라운드로 끝까지 받는다."""
    symbols: list[str] = Field(default_factory=list)   # KR:005930, US:AAPL
    indices: list[str] = Field(default_factory=list)   # INDEX:KR:0001, INDEX:US:COMP
    nasdaq100: bool = True    # 나스닥100 구성종목과 INDEX:US:NDX 를 기본으로 (KIS 미국 1분봉은 약 1개월만 남음)
    index_minutes_every: int = Field(default=60, ge=10, le=100)   # 장중 지수 1분봉 수집 주기(분). KIS 는 최근 약 100개만 줌

    @field_validator("symbols", "indices")
    @classmethod
    def _keys(cls, v: list[str]) -> list[str]:
        from central.marketdata.history import SeriesKey
        return [str(SeriesKey.parse(x)) for x in v]


_DISCORD_WEBHOOK = re.compile(r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+$")


class Settings(_Model):
    home: Path
    server: ServerConfig = Field(default_factory=ServerConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    market_data: MarketDataConfig = Field(default_factory=MarketDataConfig)
    ops: OpsConfig = Field(default_factory=OpsConfig)
    history: HistoryConfig = Field(default_factory=HistoryConfig)
    accounts: dict[str, Account]
    admin_key: SecretStr | None = None
    discord_webhook: SecretStr | None = None

    @field_validator("discord_webhook")
    @classmethod
    def _discord_url(cls, v: SecretStr | None) -> SecretStr | None:
        if v is not None and not _DISCORD_WEBHOOK.fullmatch(v.get_secret_value()):
            raise ValueError("Discord 웹훅 URL 형식 아님 (https://discord.com/api/webhooks/<id>/<token>)")
        return v

    @model_validator(mode="after")
    def _market_data_accounts_valid(self) -> Settings:
        for market, alias in (("KR", self.market_data.kr_account), ("US", self.market_data.us_account)):
            if alias is None:
                continue
            if alias not in self.accounts:
                raise ValueError(f"market_data: 계좌 '{alias}' 가 accounts 에 없음")
            if market not in self.accounts[alias].profile.markets:
                raise ValueError(f"market_data: 계좌 '{alias}' 는 {market} 미지원")
        return self

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
    def ticks_dir(self) -> Path:
        return self.data_dir / "ticks"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def backups_dir(self) -> Path:
        return self.home / "backups"

    @property
    def apps_dir(self) -> Path:
        """서브계좌 프로그램(파일럿 등)의 상태와 보고서."""
        return self.home / "apps"

    def secret_values(self) -> list[str]:
        """로그 마스킹에 등록할 비밀 문자열 목록."""
        values: list[str] = []
        for opt in (self.admin_key, self.discord_webhook):
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
            market_data=MarketDataConfig(**config.get("market_data", {})),
            ops=OpsConfig(**config.get("ops", {})),
            accounts=accounts,
            admin_key=_opt_secret(secrets.get("central", {}).get("admin_key")),
            discord_webhook=_opt_secret(secrets.get("discord", {}).get("webhook_url")),
        )
    except ValidationError as e:
        raise SettingsError(f"설정 값 오류: {_describe(e)}") from None


def _describe(e: ValidationError) -> str:
    # pydantic 기본 메시지에는 입력값이 포함될 수 있으므로 위치와 사유만 남긴다.
    return "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
