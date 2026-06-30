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

from .errors import NotAuthenticatedError
from .types import LibraryInfo


def _b64_padded_decode(value: str) -> bytes:
    """Decode a base64 string that may be missing trailing ``=`` padding."""
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.b64decode(padded)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"failed to base64-decode value: {exc}") from exc


def _find_cookie(cookies: Iterable[dict], name: str) -> dict | None:
    for c in cookies:
        if c.get("name") == name:
            return c
    return None


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
