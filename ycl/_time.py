"""UTC timestamp helpers — every timestamp this plugin writes must go through here."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def utcnow() -> datetime:
    """Current time in UTC. Use this instead of ``datetime.now()``."""
    return datetime.now(UTC)


def to_iso(dt: datetime) -> str:
    """Serialize a UTC datetime as ISO 8601 with the trailing ``Z``."""
    if dt.tzinfo is None:
        raise ValueError("Refusing to serialize naive datetime; pass a UTC-aware one.")
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def from_iso(text: str) -> datetime:
    """Parse an ISO 8601 timestamp, accepting the trailing ``Z`` or explicit offsets."""
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        # Treat naive timestamps as UTC; this matches the plugin's stated invariant.
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def resolve_expires_at(
    *,
    explicit_expires_at: str | None,
    explicit_borrowed_at: str | None,
    borrow_days: int,
    now: datetime,
) -> tuple[str, bool]:
    """Resolve the ``expires_at`` value for a loan.

    Resolution order:
      1. ``explicit_expires_at`` if provided — preferred (read off the YCL UI).
      2. ``explicit_borrowed_at + borrow_days`` if borrowed_at provided.
      3. ``now + borrow_days`` — flagged as estimated.

    Returns ``(expires_at_iso, is_estimated)``.
    """
    if explicit_expires_at:
        return to_iso(from_iso(explicit_expires_at)), False
    if explicit_borrowed_at:
        borrowed = from_iso(explicit_borrowed_at)
        return to_iso(borrowed + timedelta(days=borrow_days)), False
    return to_iso(now + timedelta(days=borrow_days)), True
