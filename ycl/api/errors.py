"""Typed errors for the YCL API client."""

from __future__ import annotations

# The installed console script that captures a session. Every "please log in"
# message names it through this constant so the hint can't drift.
LOGIN_COMMAND = "research-engine-ycl-login"


class YclApiError(Exception):
    """Base class for API-layer failures."""


class NotAuthenticatedError(YclApiError):
    """No usable session on disk. User must run ``research-engine-ycl-login``."""


class AuthExpiredError(YclApiError):
    """Cookies present but rejected (401/redirect-to-login). User must re-login."""


class BookNotBorrowedError(YclApiError):
    """The book exists but the user does not have an active loan on it."""

    def __init__(self, book_id: str, status: str) -> None:
        super().__init__(
            f"book_id={book_id!r} is not currently borrowed (status={status!r})"
        )
        self.book_id = book_id
        self.status = status


# Exact commands for the two ways the browser runtime can be missing. Wheel
# installation never downloads a browser; the operator runs this once.
CHROMIUM_INSTALL_COMMAND = "python -m playwright install chromium"
PLAYWRIGHT_INSTALL_COMMAND = 'python -m pip install "playwright>=1.40"'


class BrowserUnavailableError(YclApiError):
    """Playwright or its Chromium build is missing, so no browser can start.

    Login and catalog search need a real browser; scraping does not. ``hint``
    carries the exact command that fixes it.
    """

    def __init__(self, message: str, hint: str) -> None:
        super().__init__(message)
        self.hint = hint


def browser_launch_error(exc: BaseException) -> BrowserUnavailableError | None:
    """Translate Playwright's "browser not installed" failure, else ``None``.

    Playwright reports a missing browser build as a generic ``Error`` whose
    message says the executable doesn't exist; match that text rather than a
    private exception type.
    """
    text = str(exc)
    if "Executable doesn't exist" in text or "playwright install" in text:
        return BrowserUnavailableError(
            "Chromium for Playwright is not installed.", hint=CHROMIUM_INSTALL_COMMAND
        )
    return None
