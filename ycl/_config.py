"""Environment-driven configuration for the YourCloudLibrary plugin.

Most identity (library slug, library UUID, patron id, even the user's
default loan duration) is read from the ``__config_PROD`` cookie at
runtime — see :mod:`ycl.api.cookies`. The only thing this module reads
from the environment is an optional override for the default loan
duration when neither ``expires_at`` nor a cookie-derived value is
available.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from pydantic import BaseModel, Field


class ConfigError(RuntimeError):
    """Raised when configuration is invalid (parse failures, etc)."""


class Config(BaseModel):
    """Optional plugin-level overrides. Everything has a sensible default."""

    # Used only as a last-resort fallback when we can't infer a borrow
    # duration from the cookie's library_config or an explicit expires_at
    # passed to the tool. YCL libraries generally use 14 or 21 days.
    fallback_borrow_days: int = Field(14, ge=1, le=365)


def load() -> Config:
    """Load env-var overrides. Never raises for missing values — every
    field has a default.
    """
    load_dotenv()  # idempotent; reads .env in cwd if present
    raw_days = os.environ.get("YCL_BORROW_DAYS", "14").strip()
    try:
        borrow_days = int(raw_days)
    except ValueError as exc:
        raise ConfigError(
            f"YCL_BORROW_DAYS must be an integer; got {raw_days!r}."
        ) from exc
    return Config(fallback_borrow_days=borrow_days)
