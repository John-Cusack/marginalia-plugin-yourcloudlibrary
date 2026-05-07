"""ycl.auth_status — report whether the plugin has valid YCL session cookies."""

from __future__ import annotations

from research_engine.plugins.sdk import tool

from .._paths import COOKIE_PATH
from ..api.cookies import decode_config_cookie
from ..api.errors import NotAuthenticatedError
from ..session.cookies import CookieStore


@tool(
    id="ycl.auth_status",
    description=(
        "Report whether the plugin has YCL session cookies on disk and what "
        "library they're for. If unauthenticated, the message tells the user "
        "to run `python -m ycl.cli.login`."
    ),
    input_schema={"type": "object", "properties": {}},
)
async def handler(**_clients) -> dict:
    store = CookieStore(COOKIE_PATH)
    cookies = store.load()
    if not cookies:
        return {
            "authenticated": False,
            "cookie_path": str(COOKIE_PATH),
            "hint": "Run `uv run python -m ycl.cli.login` to authenticate.",
        }
    try:
        library = decode_config_cookie(cookies)
    except NotAuthenticatedError as exc:
        return {
            "authenticated": False,
            "cookie_path": str(COOKIE_PATH),
            "warning": str(exc),
            "hint": "Run `uv run python -m ycl.cli.login` to authenticate.",
        }
    cookie_names = sorted({c["name"] for c in cookies})
    return {
        "authenticated": True,
        "cookie_path": str(COOKIE_PATH),
        "cookie_count": len(cookies),
        "cookie_names": cookie_names,
        "library_name": library.name,
        "library_url_name": library.url_name,
        "library_uuid": library.library_uuid,
        "patron_id": library.reaktor_patron_id,
        "barcode_last4": (library.barcode or "")[-4:] if library.barcode else None,
    }
