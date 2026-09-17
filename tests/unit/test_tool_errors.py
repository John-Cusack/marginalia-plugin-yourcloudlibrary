"""Shared tool helpers: err envelope + auth/library acquisition.

Imports only ``ycl.tools._errors``, which has no SDK dependency.
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


def test_login_hints_name_the_installed_command():
    # Regression guard: hints once drifted per tool, then named a checkout-only command.
    assert "`research-engine-ycl-login`" in _errors.LOGIN_HINT
    assert "`research-engine-ycl-login`" in _errors.RELOGIN_HINT


# ----- load_library ------------------------------------------------------


def test_load_library_returns_info_on_valid_cookie(monkeypatch, plugin_paths):
    monkeypatch.setattr(_errors.CookieStore, "load", lambda self: _cookie())
    info, error = _errors.load_library(plugin_paths)
    assert error is None
    assert isinstance(info, LibraryInfo)
    assert info.url_name == "PalmBeachCountyLibrarySystem"


def test_load_library_required_errors_when_no_cookies(monkeypatch, plugin_paths):
    monkeypatch.setattr(_errors.CookieStore, "load", lambda self: [])
    info, error = _errors.load_library(plugin_paths, required=True)
    assert info is None
    assert error is not None
    assert error["error_type"] == "not_authenticated"
    assert "research-engine-ycl-login" in error["message"]


def test_load_library_optional_swallows_missing_cookies(monkeypatch, plugin_paths):
    monkeypatch.setattr(_errors.CookieStore, "load", lambda self: [])
    info, error = _errors.load_library(plugin_paths)
    assert info is None
    assert error is None          # swallowed — caller falls back to "unknown"


def test_load_library_required_errors_on_undecodable_cookie(monkeypatch, plugin_paths):
    # Cookie present but no __config_PROD → decode raises NotAuthenticatedError.
    monkeypatch.setattr(
        _errors.CookieStore, "load", lambda self: [{"name": "other", "value": "x"}]
    )
    info, error = _errors.load_library(plugin_paths, required=True)
    assert info is None
    assert error["error_type"] == "not_authenticated"


# ----- acquire_client ----------------------------------------------------


def test_acquire_client_success(monkeypatch, plugin_paths):
    sentinel = object()
    seen = []

    def _from_cookie_store(path):
        seen.append(path)
        return sentinel

    monkeypatch.setattr(_errors.YclClient, "from_cookie_store", staticmethod(_from_cookie_store))
    client, error = _errors.acquire_client(plugin_paths)
    assert client is sentinel
    assert error is None
    assert seen == [plugin_paths.cookie_path]


def test_acquire_client_not_authenticated(monkeypatch, plugin_paths):
    def _raise(path):
        raise NotAuthenticatedError("no cookie file")

    monkeypatch.setattr(_errors.YclClient, "from_cookie_store", staticmethod(_raise))
    client, error = _errors.acquire_client(plugin_paths)
    assert client is None
    assert error["error_type"] == "not_authenticated"
    assert error["hint"] == _errors.LOGIN_HINT
