"""JSON cookie persistence for YourCloudLibrary browser sessions."""

from __future__ import annotations

import json
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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(cookies, indent=2, default=str),
            encoding="utf-8",
        )
        log.debug("cookies_saved", count=len(cookies), path=str(self.path))

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()
            log.debug("cookies_cleared", path=str(self.path))
