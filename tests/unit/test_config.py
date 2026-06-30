"""Config loading: defaults, YCL_BORROW_DAYS parsing."""

from __future__ import annotations

import pytest

from ycl._config import Config, ConfigError, load


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for var in ("YCL_BORROW_DAYS",):
        monkeypatch.delenv(var, raising=False)
    yield


def test_load_defaults():
    cfg = load()
    assert cfg == Config(fallback_borrow_days=14)


def test_override_borrow_days(monkeypatch):
    monkeypatch.setenv("YCL_BORROW_DAYS", "21")
    assert load().fallback_borrow_days == 21


def test_rejects_non_integer_borrow_days(monkeypatch):
    monkeypatch.setenv("YCL_BORROW_DAYS", "fourteen")
    with pytest.raises(ConfigError, match="YCL_BORROW_DAYS"):
        load()
