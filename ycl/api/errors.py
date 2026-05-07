"""Typed errors for the YCL API client."""

from __future__ import annotations


class YclApiError(Exception):
    """Base class for API-layer failures."""


class NotAuthenticatedError(YclApiError):
    """No cookies on disk. User must run ``ycl.cli.login`` once."""


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
