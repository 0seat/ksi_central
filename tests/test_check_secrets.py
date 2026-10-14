from __future__ import annotations

from pathlib import Path

import pytest

from tools.check_secrets import forbidden_name, main, secret_lines


@pytest.mark.parametrize(
    "name",
    ["secrets.toml", "central.toml", ".env", ".env.local", "central.db", "kis_devlp.yaml", "KIS20261013", "x.pem"],
)
def test_forbidden_names(name: str) -> None:
    assert forbidden_name(Path("any") / name)


@pytest.mark.parametrize(
    "name", ["secrets.example.toml", "central.example.toml", ".env.example", "pyproject.toml", "settings.py"]
)
def test_allowed_names(name: str) -> None:
    assert not forbidden_name(Path(name))


def _file(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "sample.py"
    p.write_text(content, encoding="utf-8")
    return p


# 키와 값을 분리해 두고 실행 시 조합한다. 이 파일 자체가 검사기에 걸리지 않게 하기 위함.
@pytest.mark.parametrize(
    "template, key, value",
    [
        ('{k} = "{v}"', "app_key", "PS1234567890abcdef"),
        ("{k}='{v}'", "APP_SECRET", "abcdEFGH1234"),
        ('{{"{k}": "{v}"}}', "appkey", "PSabcdef123456"),
        ('{k} = "{v}"', "account_no", "12345678"),
        ('{k}="{v}"', "cano", "12345678"),
    ],
)
def test_literal_secret_detected(tmp_path: Path, template: str, key: str, value: str) -> None:
    assert secret_lines(_file(tmp_path, template.format(k=key, v=value)))


@pytest.mark.parametrize(
    "content",
    [
        'app_key = ""',
        'app_key = "dummy-real-app-key"',
        'account_no = "test1234"',
        "app_key = secrets.app_key",
        'app_key = "<실제 키>"',
        'account_no = "{DUMMY_NO}"',
    ],
)
def test_placeholder_allowed(tmp_path: Path, content: str) -> None:
    assert not secret_lines(_file(tmp_path, content))


def test_central_issued_keys_detected(tmp_path: Path) -> None:
    import secrets

    for prefix in ("cs_", "csadm_"):
        key = prefix + secrets.token_urlsafe(32)  # 실제 발급 형식, 실행할 때마다 무작위
        assert secret_lines(_file(tmp_path, f'client = CentralClient(url, "{key}")'))
    for text in ('os.environ["CENTRAL_API_KEY"]', 'headers={"X-API-Key": "cs_nope"}', 'cs_short_value'):
        assert not secret_lines(_file(tmp_path, text))


def test_main_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = _file(tmp_path, 'app_key = ""')
    assert main([str(good)]) == 0

    value = "realLookingSecret99"
    bad = tmp_path / "leak.py"
    bad.write_text('app_secret = "{}"'.format(value), encoding="utf-8")
    assert main([str(bad)]) == 1
    err = capsys.readouterr().err
    assert value not in err  # 값 자체는 출력하지 않음
    assert "app_secret" in err
