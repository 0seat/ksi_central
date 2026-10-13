from __future__ import annotations

from pathlib import Path

import pytest

CENTRAL_TOML = """
[server]
hosts = ["127.0.0.1"]
port = 8700

[accounts.paper]
env = "paper"
markets = ["KR"]
kr_routes = ["KRX"]
kr_default_route = "KRX"
rate_limit_per_sec = 2

[accounts.real]
env = "real"
markets = ["KR", "US"]
kr_routes = ["KRX", "NXT", "SOR"]
kr_default_route = "SOR"
integrated_margin = true
rate_limit_per_sec = 18
"""

# 계좌번호는 8자리 숫자여야 해서 dummy 접두어를 붙일 수 없다.
# 비밀 검사기(check_secrets, gitleaks)에 걸리지 않도록 키 이름과 분리된 상수로 둔다.
DUMMY_PAPER_NO = "50000001"
DUMMY_REAL_NO = "60000002"

SECRETS_TOML = f"""
[central]
admin_key = "dummy-admin-key-0000000000"

[accounts.paper]
app_key = "dummy-paper-app-key"
app_secret = "dummy-paper-app-secret"
hts_id = "dummyhts"
account_no = "{DUMMY_PAPER_NO}"
product_code = "01"

[accounts.real]
app_key = "dummy-real-app-key"
app_secret = "dummy-real-app-secret"
hts_id = "dummyhts"
account_no = "{DUMMY_REAL_NO}"
product_code = "01"
"""


def write_home(home: Path, central: str = CENTRAL_TOML, secrets: str = SECRETS_TOML) -> Path:
    (home / "config").mkdir(parents=True, exist_ok=True)
    (home / "secrets").mkdir(parents=True, exist_ok=True)
    (home / "config" / "central.toml").write_text(central, encoding="utf-8")
    (home / "secrets" / "secrets.toml").write_text(secrets, encoding="utf-8")
    return home


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = write_home(tmp_path / "central_home")
    monkeypatch.setenv("CENTRAL_HOME", str(h))
    return h
