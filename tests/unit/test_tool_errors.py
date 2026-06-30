"""Shared tool helpers: err envelope + auth/library acquisition.

Imports only ``ycl.tools._errors`` (no research_engine SDK), so it runs in the
plain unit-test environment that never loads the tool handlers themselves.
"""

from __future__ import annotations

import base64
import json

from ycl.api.errors import NotAuthenticatedError
from ycl.api.types import LibraryInfo
from ycl.tools import _errors

_PAYLOAD = {
    "library_info": {
        "name": "Palm Beach County Library System",
        "urlName": "PalmBeachCountyLibrarySystem",
    },
    "library_config": {"reaktor_patron_id": 1},
    "login_info": {"library": "uuid", "barcode": "X", "state": "FL"},
}


def _cookie() -> list[dict]:
    value = base64.b64encode(json.dumps(_PAYLOAD).encode()).decode("ascii")
    return [{"name": "__config_PROD", "value": value}]


def test_err_envelope():
    out = _errors.err("api_error", "boom", book_id="onc1")
    assert out == {
        "status": "error",
        "error_type": "api_error",
        "message": "boom",
        "book_id": "onc1",
    }


def test_login_hints_carry_uv_run():
    # Regression guard for the drifted ingest_book hint.
    assert "uv run python -m ycl.cli.login" in _errors.LOGIN_HINT
    assert "uv run python -m ycl.cli.login" in _errors.RELOGIN_HINT


# ----- load_library ------------------------------------------------------


def test_load_library_returns_info_on_valid_cookie(monkeypatch):
    monkeypatch.setattr(_errors.CookieStore, "load", lambda self: _cookie())
    info, error = _errors.load_library()
    assert error is None
    assert isinstance(info, LibraryInfo)
    assert info.url_name == "PalmBeachCountyLibrarySystem"


def test_load_library_required_errors_when_no_cookies(monkeypatch):
    monkeypatch.setattr(_errors.CookieStore, "load", lambda self: [])
    info, error = _errors.load_library(required=True)
    assert info is None
    assert error is not None
    assert error["error_type"] == "not_authenticated"
    assert "uv run python -m ycl.cli.login" in error["message"]


def test_load_library_optional_swallows_missing_cookies(monkeypatch):
    monkeypatch.setattr(_errors.CookieStore, "load", lambda self: [])
    info, error = _errors.load_library()
    assert info is None
    assert error is None          # swallowed — caller falls back to "unknown"


def test_load_library_required_errors_on_undecodable_cookie(monkeypatch):
    # Cookie present but no __config_PROD → decode raises NotAuthenticatedError.
    monkeypatch.setattr(
        _errors.CookieStore, "load", lambda self: [{"name": "other", "value": "x"}]
    )
    info, error = _errors.load_library(required=True)
    assert info is None
    assert error["error_type"] == "not_authenticated"


# ----- acquire_client ----------------------------------------------------


def test_acquire_client_success(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(
        _errors.YclClient, "from_cookie_store", staticmethod(lambda: sentinel)
    )
    client, error = _errors.acquire_client()
    assert client is sentinel
    assert error is None


def test_acquire_client_not_authenticated(monkeypatch):
    def _raise():
        raise NotAuthenticatedError("no cookie file")

    monkeypatch.setattr(
        _errors.YclClient, "from_cookie_store", staticmethod(_raise)
    )
    client, error = _errors.acquire_client()
    assert client is None
    assert error["error_type"] == "not_authenticated"
    assert error["hint"] == _errors.LOGIN_HINT
