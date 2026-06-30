"""Shared error/auth helpers for the MCP tool handlers.

Centralizes three things the tools used to each reimplement:
- the ``{"status": "error", ...}`` envelope (``err``),
- the live-API client acquisition + "not authenticated" translation
  (``acquire_client``, for tools that hit the YCL API),
- the local cookie/library decode (``load_library``, for tools that only need
  the library identity from the ``__config_PROD`` cookie).

Deliberately free of any ``research_engine`` SDK import so it stays importable
in the unit-test environment, which never imports the tool handlers themselves.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .._paths import COOKIE_PATH
from ..api import NotAuthenticatedError, YclClient
from ..api.cookies import decode_config_cookie
from ..session.cookies import CookieStore

if TYPE_CHECKING:
    from ..api.types import LibraryInfo

# Single source of truth for the login hints, so they can't drift per-tool.
LOGIN_HINT = "Run `uv run python -m ycl.cli.login` once."
RELOGIN_HINT = "Re-run `uv run python -m ycl.cli.login`."


def err(error_type: str, message: str, **extra) -> dict:
    """Build the standard tool error envelope."""
    return {"status": "error", "error_type": error_type, "message": message, **extra}


def acquire_client() -> tuple[YclClient | None, dict | None]:
    """Construct a cookie-authenticated :class:`YclClient` for the live-API tools.

    Returns ``(client, None)`` on success or ``(None, err_dict)`` when the user
    has never logged in, so callers can ``if error: return error``.
    """
    try:
        return YclClient.from_cookie_store(), None
    except NotAuthenticatedError as exc:
        return None, err("not_authenticated", str(exc), hint=LOGIN_HINT)


def load_library(*, required: bool = False) -> tuple[LibraryInfo | None, dict | None]:
    """Decode the library identity from the ``__config_PROD`` cookie.

    For the local-store tools that don't hit the live API. With
    ``required=False`` (default) a missing/invalid cookie is swallowed and the
    caller falls back to a ``"unknown"`` library key; with ``required=True`` it
    yields a ``not_authenticated`` error dict instead.
    """
    cookies = CookieStore(COOKIE_PATH).load()
    if not cookies:
        if required:
            return None, err("not_authenticated", "No cookies on disk. " + LOGIN_HINT)
        return None, None
    try:
        return decode_config_cookie(cookies), None
    except NotAuthenticatedError as exc:
        if required:
            return None, err("not_authenticated", str(exc))
        return None, None
