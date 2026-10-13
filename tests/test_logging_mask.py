from __future__ import annotations

import logging
from pathlib import Path

import pytest

from central.core.logging import MASK, MaskingFormatter, SecretMasker, masker, setup_logging
from central.settings import load_settings


@pytest.fixture
def m() -> SecretMasker:
    sm = SecretMasker()
    sm.add(["60000002", "60000002-01", "dummy-real-app-secret"])
    return sm


def test_registered_values_masked(m: SecretMasker) -> None:
    out = m.mask("account 60000002-01 secret dummy-real-app-secret ok")
    assert "60000002" not in out
    assert "dummy-real-app-secret" not in out
    assert out.count(MASK) == 2


def test_longest_first(m: SecretMasker) -> None:
    assert m.mask("60000002-01") == MASK


# 키와 값을 분리해 두고 실행 시 조합한다. 소스에 "키 = 값" 리터럴이 생기면 비밀 검사기에 걸리기 때문.
@pytest.mark.parametrize(
    "template, key, leaked",
    [
        ('{{"{k}": "{v}", "x": 1}}', "appkey", "unregisteredKEY123"),
        ("{k}={v}&grant_type=client", "appsecret", "abcdefgh12345"),
        ("{k}: Bearer {v}", "Authorization", "opaque-bearer-value"),
        ("'{k}': '{v}'", "access_token", "tok-123456"),
        ("{k}={v}", "approval_key", "aaaa-bbbb-cccc"),
    ],
)
def test_key_value_patterns_masked(m: SecretMasker, template: str, key: str, leaked: str) -> None:
    out = m.mask(template.format(k=key, v=leaked))
    assert leaked not in out
    assert MASK in out


def test_short_values_ignored() -> None:
    sm = SecretMasker()
    sm.add(["01", ""])
    assert sm.mask("product 01") == "product 01"


def test_formatter_masks_exceptions(m: SecretMasker, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("central.core.logging.masker", m)
    fmt = MaskingFormatter("%(message)s")
    try:
        raise RuntimeError("failed for 60000002-01")
    except RuntimeError:
        import sys

        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "order %s", ("60000002",), sys.exc_info())
    out = fmt.format(record)
    assert "60000002" not in out
    assert "RuntimeError" in out


def test_setup_logging_writes_masked_file(home: Path) -> None:
    settings = load_settings()
    masker.clear()
    try:
        setup_logging(settings)
        logging.getLogger("t").warning("key=%s", "dummy-real-app-key")
        for h in logging.getLogger().handlers:
            h.flush()
        content = (settings.logs_dir / "central.log").read_text(encoding="utf-8")
        assert "dummy-real-app-key" not in content
        assert MASK in content
    finally:
        for h in logging.getLogger().handlers[:]:
            h.close()
            logging.getLogger().removeHandler(h)
        masker.clear()
