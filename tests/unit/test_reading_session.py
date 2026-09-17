"""Unit tests for reading-session (scrape-capability) expiry detection."""
from __future__ import annotations

from ycl.api.cookies import reading_session_status

_NOW = 1_700_000_000.0


def _ck(name, expires):
    return {"name": name, "value": "x", "domain": ".yourcloudlibrary.com", "expires": expires}


def test_missing_session_cookie():
    s = reading_session_status([_ck("__config_PROD", _NOW + 100)], now=_NOW)
    assert s["ok"] is False and s["reason"] == "missing_session_cookie"


def test_expired_session():
    s = reading_session_status([_ck("__session_PROD", _NOW - 1)], now=_NOW)
    assert s["ok"] is False and s["reason"] == "session_expired"
    assert "login" in s["detail"].lower()


def test_valid_session():
    s = reading_session_status([_ck("__session_PROD", _NOW + 86400)], now=_NOW)
    assert s["ok"] is True and s["reason"] == "ok"


def test_session_cookie_without_expiry_is_ok():
    # expires <= 0 / None means a browser "session" cookie — treat as not-expired.
    s = reading_session_status([_ck("__session_PROD", -1)], now=_NOW)
    assert s["ok"] is True
