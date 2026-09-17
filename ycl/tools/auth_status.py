"""yourcloudlibrary.auth_status — report whether the plugin has valid YCL session cookies."""

from __future__ import annotations

from typing import TYPE_CHECKING

from research_engine_sdk import tool

from .._paths import resolve_paths
from .._time import utcnow
from ..api.cookies import (
    decode_config_cookie,
    reading_session_status,
    session_expiry_status,
)
from ..api.errors import NotAuthenticatedError
from ..session.cookies import CookieStore
from ._errors import LOGIN_HINT

if TYPE_CHECKING:
    from research_engine_sdk import PluginContext


@tool(
    id="yourcloudlibrary.auth_status",
    description=(
        "Report whether the plugin has YCL session cookies on disk and what "
        "library they're for, and whether the short-lived reading session can "
        "still fetch book content. If unauthenticated, the hint names the "
        "research-engine-ycl-login command."
    ),
    input_schema={"type": "object", "properties": {}},
)
async def handler(context: PluginContext | None = None, **_clients) -> dict:
    cookie_path = resolve_paths(context).cookie_path
    cookies = CookieStore(cookie_path).load()
    if not cookies:
        return {
            "authenticated": False,
            "cookie_path": str(cookie_path),
            "hint": LOGIN_HINT,
        }
    try:
        library = decode_config_cookie(cookies)
    except NotAuthenticatedError as exc:
        return {
            "authenticated": False,
            "cookie_path": str(cookie_path),
            "warning": str(exc),
            "hint": LOGIN_HINT,
        }
    cookie_names = sorted({c["name"] for c in cookies})
    reading = reading_session_status(cookies)
    return {
        # Cookies present + decodable: catalog search/borrow should work.
        "authenticated": True,
        # Reading/ingest needs an UNEXPIRED __session_PROD; epubservice enforces
        # it strictly while the catalog is lenient. This is the signal that
        # decides whether scrape/acquire_and_ingest will work.
        "can_read": reading["ok"],
        "reading_status": reading["reason"],
        "reading_hint": None if reading["ok"] else reading["detail"],
        "cookie_path": str(cookie_path),
        "cookie_count": len(cookies),
        "cookie_names": cookie_names,
        "library_name": library.name,
        "library_url_name": library.url_name,
        "library_uuid": library.library_uuid,
        "patron_id": library.reaktor_patron_id,
        "barcode_last4": (library.barcode or "")[-4:] if library.barcode else None,
        **session_expiry_status(cookies, now=utcnow()),
    }
