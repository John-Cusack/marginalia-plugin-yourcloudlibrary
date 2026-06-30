"""Cookie-decoding helpers.

YCL's ``__config_PROD`` cookie is a base64-encoded JSON document with the
patron's library info, library UUID, barcode, and state. Decoding it lets
us derive everything the plugin previously asked the user to put in
``.env`` — slug, library UUID, default loan duration, etc. — without
explicit configuration.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Iterable
from datetime import UTC, datetime

from .._time import to_iso
from .errors import NotAuthenticatedError
from .types import LibraryInfo

SESSION_COOKIE = "__session_PROD"

# How many days out we start nagging the user to re-login. The session cookie
# is ~30 days; a one-week heads-up gives plenty of time to re-run the CLI
# before a capture window slams shut.
SESSION_WARN_DAYS = 7


def _b64_padded_decode(value: str, *, urlsafe: bool = False) -> bytes:
    """Decode a base64 string that may be missing trailing ``=`` padding.

    Set ``urlsafe=True`` for the base64url alphabet used by JWT segments.
    """
    padded = value + "=" * (-len(value) % 4)
    decoder = base64.urlsafe_b64decode if urlsafe else base64.b64decode
    try:
        return decoder(padded)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"failed to base64-decode value: {exc}") from exc


def _find_cookie(cookies: Iterable[dict], name: str) -> dict | None:
    for c in cookies:
        if c.get("name") == name:
            return c
    return None


def has_session_cookie(cookies: Iterable[dict]) -> bool:
    """True if the ``__session_PROD`` cookie is present with a non-empty value."""
    cookie = _find_cookie(cookies, SESSION_COOKIE)
    return bool(cookie and cookie.get("value"))


def session_expiry(cookies: Iterable[dict]) -> datetime | None:
    """Return the ``__session_PROD`` JWT's ``exp`` as a UTC datetime.

    The session cookie value is a JWT (``header.payload.signature``); the
    payload carries an ``exp`` Unix timestamp. We decode the claim without
    verifying the signature — we only want the expiry, and we hold no key.
    Returns ``None`` if the cookie is missing or the token can't be decoded
    (so callers can degrade to "expiry unknown" rather than crash).
    """
    cookie = _find_cookie(cookies, SESSION_COOKIE)
    if cookie is None:
        return None
    token = str(cookie.get("value", ""))
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = json.loads(_b64_padded_decode(parts[1], urlsafe=True))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    exp = payload.get("exp") if isinstance(payload, dict) else None
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        return None
    try:
        return datetime.fromtimestamp(exp, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def session_expiry_status(cookies: Iterable[dict], *, now: datetime) -> dict:
    """Build a JSON-friendly description of the session's expiry state.

    Always returns the three ``session_*`` keys; adds ``session_warning`` when
    the session has expired or is within :data:`SESSION_WARN_DAYS` of doing so.
    Shared by ``ycl.auth_status`` and ``ycl.list_books``.
    """
    expiry = session_expiry(cookies)
    if expiry is None:
        return {
            "session_expires_at": None,
            "session_expires_in_days": None,
            "session_expired": None,
        }
    delta = expiry - now
    expired = expiry <= now
    # Floor division of a negative delta would report e.g. -1 days for an
    # already-expired session, which reads as nonsense next to the "expires
    # in" framing — clamp at 0 and let session_expired carry the past state.
    days = max(0, int(delta.total_seconds() // 86400))
    out: dict = {
        "session_expires_at": to_iso(expiry),
        "session_expires_in_days": days,
        "session_expired": expired,
    }
    if expired:
        out["session_warning"] = (
            "Session has expired — re-run `python -m ycl.cli.login`."
        )
    elif days <= SESSION_WARN_DAYS:
        unit = "day" if days == 1 else "days"
        out["session_warning"] = (
            f"Session expires in {days} {unit} — re-run "
            "`python -m ycl.cli.login` soon."
        )
    return out


def decode_config_cookie(cookies: Iterable[dict]) -> LibraryInfo:
    """Extract :class:`LibraryInfo` from ``__config_PROD``.

    Raises :class:`NotAuthenticatedError` if the cookie is missing — that's
    the canonical "user has never logged in" signal in this plugin.
    """
    config = _find_cookie(cookies, "__config_PROD")
    if config is None:
        raise NotAuthenticatedError(
            "__config_PROD cookie missing — run ycl.cli.login to authenticate."
        )
    raw = _b64_padded_decode(str(config.get("value", "")))
    text = raw.decode("utf-8", errors="replace")
    # The cookie body is "<JSON object><binary noise>" — sometimes the noise
    # contains stray brace bytes, so we can't just rfind('}'). Use
    # raw_decode to consume exactly one JSON object starting at the first '{'.
    start = text.find("{")
    if start == -1:
        raise ValueError("malformed __config_PROD cookie: no JSON object found")
    try:
        payload, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed __config_PROD cookie: {exc}") from exc
    info = payload.get("library_info") or {}
    cfg = payload.get("library_config") or {}
    login = payload.get("login_info") or {}
    return LibraryInfo(
        name=info.get("name") or "",
        url_name=info.get("urlName") or "",
        library_uuid=login.get("library") or "",
        reaktor_patron_id=cfg.get("reaktor_patron_id"),
        barcode=login.get("barcode"),
        state=login.get("state"),
    )


def cookies_to_jar(cookies: Iterable[dict]) -> dict[str, str]:
    """Convert Playwright-style cookie dicts to an httpx-friendly mapping.

    Only cookies on a ``yourcloudlibrary.com`` domain are kept.
    """
    jar: dict[str, str] = {}
    for c in cookies:
        domain = (c.get("domain") or "").lstrip(".")
        if "yourcloudlibrary.com" not in domain:
            continue
        name = c.get("name")
        value = c.get("value")
        if name and value is not None:
            jar[name] = value
    return jar
