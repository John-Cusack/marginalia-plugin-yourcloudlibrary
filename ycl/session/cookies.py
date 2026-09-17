"""JSON cookie persistence for YourCloudLibrary browser sessions."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)


class CookieStore:
    """Load and save Playwright cookies as JSON."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> list[dict]:
        if not self.path.exists():
            log.debug("no_cookie_file", path=str(self.path))
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
            log.warning("cookie_file_not_a_list", path=str(self.path))
            return []
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("cookie_load_failed", path=str(self.path), error=str(exc))
            return []

    def save(self, cookies: list[dict]) -> None:
        """Write atomically, readable only by the owner.

        The file is a live library session. It is created 0600 from the first
        byte (never written world-readable and then chmod-ed), and swapped in
        with ``os.replace`` so a crash can't leave a truncated session behind.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".cookies-", suffix=".json.tmp"
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(cookies, fh, indent=2, default=str)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        log.debug("cookies_saved", count=len(cookies), path=str(self.path))

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()
            log.debug("cookies_cleared", path=str(self.path))
