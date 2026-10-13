from __future__ import annotations

from pathlib import Path

import pytest

from central.settings import REPO_ROOT, SettingsError, load_settings, resolve_home
from tests.conftest import CENTRAL_TOML, SECRETS_TOML, write_home


def test_load_settings(home: Path) -> None:
    s = load_settings()
    assert s.home == home.resolve()
    assert set(s.accounts) == {"paper", "real"}
    real = s.accounts["real"]
    assert real.profile.env == "real"
    assert real.profile.kr_default_route == "SOR"
    assert real.profile.integrated_margin is True
    assert real.profile.trading_enabled is False
    assert real.secrets.app_key.get_secret_value() == "dummy-real-app-key"
    assert s.tokens_dir == home.resolve() / "state" / "tokens"


def test_secrets_not_in_repr(home: Path) -> None:
    text = repr(load_settings()) + str(load_settings())
    for value in ("dummy-real-app-key", "dummy-real-app-secret", "60000002", "dummy-admin-key"):
        assert value not in text


def test_secret_values_include_account_formats(home: Path) -> None:
    values = load_settings().secret_values()
    assert "60000002" in values
    assert "60000002-01" in values
    assert "dummy-real-app-secret" in values


def test_home_inside_repo_rejected() -> None:
    with pytest.raises(SettingsError, match="저장소 안"):
        resolve_home(REPO_ROOT / "somewhere")
    with pytest.raises(SettingsError):
        resolve_home(REPO_ROOT)


def test_missing_files(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="설정 파일 없음"):
        load_settings(tmp_path)


def test_alias_mismatch(tmp_path: Path) -> None:
    secrets = SECRETS_TOML.split("[accounts.real]")[0]
    write_home(tmp_path, secrets=secrets)
    with pytest.raises(SettingsError, match="alias 불일치"):
        load_settings(tmp_path)


def test_empty_secret_rejected_without_leaking(tmp_path: Path) -> None:
    write_home(tmp_path, secrets=SECRETS_TOML.replace('app_key = "dummy-real-app-key"', 'app_key = ""'))
    with pytest.raises(SettingsError, match="'real'.*app_key") as exc:
        load_settings(tmp_path)
    assert "dummy-real-app-secret" not in str(exc.value)


def test_bad_account_no_does_not_leak_value(tmp_path: Path) -> None:
    write_home(tmp_path, secrets=SECRETS_TOML.replace('"60000002"', '"6000-0002x"'))
    with pytest.raises(SettingsError, match="account_no") as exc:
        load_settings(tmp_path)
    assert "6000-0002x" not in str(exc.value)


def test_default_route_must_be_supported(tmp_path: Path) -> None:
    write_home(tmp_path, central=CENTRAL_TOML.replace('kr_default_route = "KRX"', 'kr_default_route = "SOR"'))
    with pytest.raises(SettingsError, match="kr_default_route"):
        load_settings(tmp_path)


def test_wildcard_bind_rejected(tmp_path: Path) -> None:
    write_home(tmp_path, central=CENTRAL_TOML.replace('hosts = ["127.0.0.1"]', 'hosts = ["0.0.0.0"]'))
    with pytest.raises(SettingsError, match="hosts"):
        load_settings(tmp_path)


def test_example_templates_parse_but_require_values(tmp_path: Path) -> None:
    """저장소의 예시 파일은 형식은 맞지만, 빈 키 때문에 그대로는 로드되지 않아야 한다."""
    central = (REPO_ROOT / "config" / "central.example.toml").read_text(encoding="utf-8")
    secrets = (REPO_ROOT / "config" / "secrets.example.toml").read_text(encoding="utf-8")
    write_home(tmp_path, central=central, secrets=secrets)
    with pytest.raises(SettingsError, match="비어 있음"):
        load_settings(tmp_path)
