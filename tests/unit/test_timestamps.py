"""Timestamp helpers: UTC round-trip, expires_at resolution, naive rejection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from ycl._time import from_iso, resolve_expires_at, to_iso, utcnow


def test_utcnow_is_aware_and_utc():
    now = utcnow()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_to_iso_emits_z_suffix():
    dt = datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC)
    assert to_iso(dt) == "2026-05-07T12:00:00Z"


def test_to_iso_rejects_naive():
    with pytest.raises(ValueError):
        to_iso(datetime(2026, 5, 7, 12, 0, 0))


def test_to_iso_normalizes_offset_to_utc():
    eastern = timezone(timedelta(hours=-5))
    dt = datetime(2026, 5, 7, 7, 0, 0, tzinfo=eastern)  # 12:00 UTC
    assert to_iso(dt) == "2026-05-07T12:00:00Z"


def test_from_iso_parses_z_suffix():
    dt = from_iso("2026-05-07T12:00:00Z")
    assert dt == datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC)


def test_from_iso_parses_explicit_offset():
    dt = from_iso("2026-05-07T07:00:00-05:00")
    assert dt == datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC)


def test_from_iso_treats_naive_as_utc():
    dt = from_iso("2026-05-07T12:00:00")
    assert dt == datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC)


def test_resolve_expires_at_prefers_explicit_expires_at():
    now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC)
    out, estimated = resolve_expires_at(
        explicit_expires_at="2026-05-21T12:00:00Z",
        explicit_borrowed_at="2026-05-01T12:00:00Z",
        borrow_days=14,
        now=now,
    )
    assert out == "2026-05-21T12:00:00Z"
    assert estimated is False


def test_resolve_expires_at_uses_borrowed_at_plus_days():
    now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC)
    out, estimated = resolve_expires_at(
        explicit_expires_at=None,
        explicit_borrowed_at="2026-05-01T12:00:00Z",
        borrow_days=14,
        now=now,
    )
    assert out == "2026-05-15T12:00:00Z"
    assert estimated is False


def test_resolve_expires_at_falls_back_to_now_plus_days_estimated():
    now = datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC)
    out, estimated = resolve_expires_at(
        explicit_expires_at=None,
        explicit_borrowed_at=None,
        borrow_days=14,
        now=now,
    )
    assert out == "2026-05-21T12:00:00Z"
    assert estimated is True
