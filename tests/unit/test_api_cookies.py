"""Cookie decoding helpers: __config_PROD parse + jar conversion."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest

from ycl.api.cookies import (
    cookies_to_jar,
    decode_config_cookie,
    has_session_cookie,
    session_expiry,
    session_expiry_status,
)
from ycl.api.errors import NotAuthenticatedError


def _encode_config(payload: dict) -> str:
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


def _b64url(obj: dict) -> str:
    raw = json.dumps(obj).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _make_jwt(*, exp: int | None) -> str:
    """A signature-less JWT good enough to read the exp claim from."""
    payload: dict = {"sub": "patron"}
    if exp is not None:
        payload["exp"] = exp
    return f"{_b64url({'alg': 'HS256', 'typ': 'JWT'})}.{_b64url(payload)}.sig"


_SAMPLE_PAYLOAD = {
    "library_info": {
        "name": "Palm Beach County Library System",
        "urlName": "PalmBeachCountyLibrarySystem",
        "url": "https://ebook.yourcloudlibrary.com/library/PalmBeachCountyLibrarySystem",
    },
    "library_config": {"reaktor_patron_id": 203592612},
    "login_info": {
        "barcode": "D027150451",
        "pin": None,
        "library": "793edfa10e6743fc8ce5cf6b1b4147bf",
        "state": "FL",
    },
}


def test_decode_config_cookie_extracts_identity():
    cookies = [{"name": "__config_PROD", "value": _encode_config(_SAMPLE_PAYLOAD)}]
    info = decode_config_cookie(cookies)
    assert info.name == "Palm Beach County Library System"
    assert info.url_name == "PalmBeachCountyLibrarySystem"
    assert info.library_uuid == "793edfa10e6743fc8ce5cf6b1b4147bf"
    assert info.reaktor_patron_id == 203592612
    assert info.barcode == "D027150451"
    assert info.state == "FL"


def test_decode_config_cookie_handles_trailing_garbage():
    """The real cookie body has binary noise after the JSON object — drop it."""
    payload_b64 = _encode_config(_SAMPLE_PAYLOAD)
    # Append base64 of arbitrary bytes after the payload.
    noisy = payload_b64 + base64.b64encode(b"\x00\xff\xc4\x84").decode("ascii")
    cookies = [{"name": "__config_PROD", "value": noisy}]
    info = decode_config_cookie(cookies)
    assert info.url_name == "PalmBeachCountyLibrarySystem"


def test_decode_config_cookie_ignores_brace_bytes_in_noise():
    """Real-world failure case: trailing noise contains `}` bytes which
    fooled an earlier ``rfind('}')`` strategy. ``raw_decode`` must consume
    exactly one JSON object regardless of whatever junk follows."""
    payload_json = json.dumps(_SAMPLE_PAYLOAD).encode("utf-8")
    # Build raw bytes = payload + brace-laden noise, then base64 the whole
    # thing the way the YCL server does.
    noise = b"\x00}\xff\x88}}\x01\x02"
    cookie_value = base64.b64encode(payload_json + noise).decode("ascii")
    cookies = [{"name": "__config_PROD", "value": cookie_value}]
    info = decode_config_cookie(cookies)
    assert info.url_name == "PalmBeachCountyLibrarySystem"
    assert info.barcode == "D027150451"


def test_decode_config_cookie_missing_raises_not_authenticated():
    with pytest.raises(NotAuthenticatedError):
        decode_config_cookie([])


def test_cookies_to_jar_filters_to_yourcloudlibrary_only():
    cookies = [
        {"name": "session", "value": "v1", "domain": ".yourcloudlibrary.com"},
        {"name": "tracker", "value": "v2", "domain": ".other.com"},
        {"name": "config", "value": "v3", "domain": "epubservice.yourcloudlibrary.com"},
    ]
    jar = cookies_to_jar(cookies)
    assert jar == {"session": "v1", "config": "v3"}


def test_cookies_to_jar_skips_missing_values():
    cookies = [
        {"name": "ok", "value": "v", "domain": ".yourcloudlibrary.com"},
        {"name": "no_value", "domain": ".yourcloudlibrary.com"},
        {"value": "no_name", "domain": ".yourcloudlibrary.com"},
    ]
    jar = cookies_to_jar(cookies)
    assert jar == {"ok": "v"}


# --- session cookie presence + expiry (P2.1, P2.3) -----------------------


def test_has_session_cookie_detects_presence():
    assert has_session_cookie([{"name": "__session_PROD", "value": "tok"}])
    assert not has_session_cookie([{"name": "__config_PROD", "value": "x"}])
    # Present but empty value doesn't count.
    assert not has_session_cookie([{"name": "__session_PROD", "value": ""}])


def test_session_expiry_decodes_jwt_exp():
    exp = int(datetime(2026, 7, 1, tzinfo=UTC).timestamp())
    cookies = [{"name": "__session_PROD", "value": _make_jwt(exp=exp)}]
    decoded = session_expiry(cookies)
    assert decoded == datetime(2026, 7, 1, tzinfo=UTC)


def test_session_expiry_none_when_missing_or_undecodable():
    assert session_expiry([]) is None
    # Not a 3-part JWT.
    assert session_expiry([{"name": "__session_PROD", "value": "not-a-jwt"}]) is None
    # JWT with no exp claim.
    no_exp = [{"name": "__session_PROD", "value": _make_jwt(exp=None)}]
    assert session_expiry(no_exp) is None


def test_session_expiry_status_warns_when_expiring_soon():
    now = datetime(2026, 6, 29, tzinfo=UTC)
    exp = int((now + timedelta(days=3)).timestamp())
    cookies = [{"name": "__session_PROD", "value": _make_jwt(exp=exp)}]
    status = session_expiry_status(cookies, now=now)
    assert status["session_expired"] is False
    assert status["session_expires_in_days"] == 3
    assert "expires in 3 days" in status["session_warning"]


def test_session_expiry_status_no_warning_when_far_out():
    now = datetime(2026, 6, 29, tzinfo=UTC)
    exp = int((now + timedelta(days=20)).timestamp())
    cookies = [{"name": "__session_PROD", "value": _make_jwt(exp=exp)}]
    status = session_expiry_status(cookies, now=now)
    assert status["session_expired"] is False
    assert status["session_expires_in_days"] == 20
    assert "session_warning" not in status


def test_session_expiry_status_flags_expired():
    now = datetime(2026, 6, 29, tzinfo=UTC)
    exp = int((now - timedelta(days=1)).timestamp())
    cookies = [{"name": "__session_PROD", "value": _make_jwt(exp=exp)}]
    status = session_expiry_status(cookies, now=now)
    assert status["session_expired"] is True
    assert "expired" in status["session_warning"].lower()


def test_session_expiry_status_unknown_when_no_session_cookie():
    now = datetime(2026, 6, 29, tzinfo=UTC)
    status = session_expiry_status([{"name": "__config_PROD", "value": "x"}], now=now)
    assert status["session_expires_at"] is None
    assert status["session_expires_in_days"] is None
    assert status["session_expired"] is None
    assert "session_warning" not in status
