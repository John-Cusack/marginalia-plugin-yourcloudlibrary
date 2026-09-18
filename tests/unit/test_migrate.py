"""research-engine-ycl-login migrate — inventory, refuse on conflict, verify, clean up."""

from __future__ import annotations

import base64
import json
import os
import stat
import sys

import pytest

import ycl.cli.migrate as migrate
from ycl._paths import PluginPaths
from ycl.borrows import BorrowStore

SESSION_VALUE = "super-secret-session-value"


def _config_value() -> str:
    payload = {"library_info": {"name": "Test Library", "urlName": "TestLib"},
               "login_info": {"library": "uuid"}}
    return base64.b64encode(json.dumps(payload).encode()).decode()


@pytest.fixture
def old(tmp_path):
    """A populated pre-0.3.0 data directory."""
    paths = PluginPaths(tmp_path / "old")
    paths.data_dir.mkdir(parents=True)
    paths.cookie_path.write_text(json.dumps([
        {"name": "__config_PROD", "value": _config_value()},
        {"name": "__session_PROD", "value": SESSION_VALUE},
    ]))
    os.chmod(paths.cookie_path, 0o664)  # how 0.2.0 left it
    store = BorrowStore(paths.borrows_path)
    store.upsert(library_id="TestLib", book_id="b1", title="One")
    store.upsert(library_id="TestLib", book_id="b2", title="Two")
    text = paths.text_path_for("TestLib", "b1")
    text.parent.mkdir(parents=True)
    text.write_text("Licensed book text.")
    paths.chapters_path_for("TestLib", "b1").write_text('{"title": "One", "chapters": []}')
    paths.partial_path_for("TestLib", "b2").write_text("partial")
    return paths


@pytest.fixture
def new(tmp_path):
    return PluginPaths(tmp_path / "new")


@pytest.fixture(autouse=True)
def not_a_tty(monkeypatch):
    class _Stdin:
        def isatty(self):
            return False

        def readline(self):
            return ""

    monkeypatch.setattr(sys, "stdin", _Stdin())


def _run(old, new, *extra):
    return migrate.run(["--from", str(old.data_dir), "--data-dir", str(new.data_dir), *extra])


def _data_files(paths: PluginPaths) -> dict[str, bytes]:
    return {
        str(p.relative_to(paths.data_dir)): p.read_bytes()
        for p in paths.data_dir.rglob("*")
        if p.is_file() and p.name != "borrows.json.lock"
    }


def test_absent_old_directory_is_a_no_op(tmp_path, new, capsys):
    assert migrate.run(["--from", str(tmp_path / "missing"), "--data-dir", str(new.data_dir)]) == 0
    assert "Nothing to migrate" in capsys.readouterr().out
    assert not new.data_dir.exists()


def test_inventory_classifies_every_kind(old, new):
    kinds = {item.source.name: item.kind for item in migrate.plan(old.data_dir, new.data_dir)}
    assert kinds == {
        "cookies.json": "session cookies",
        "borrows.json": "borrow registry",
        "b1.txt": "extracted text",
        "b1.chapters.json": "chapter sidecar",
        "b2.partial.txt": "partial scrape",
    }


def test_successful_migration_copies_verifies_and_keeps_originals(old, new, capsys):
    before = _data_files(old)

    assert _run(old, new, "--yes") == 0

    assert _data_files(new) == before
    assert _data_files(old) == before  # originals kept without --remove-old
    assert stat.S_IMODE(new.cookie_path.stat().st_mode) == 0o600
    assert BorrowStore(new.borrows_path).get("TestLib", "b2")["title"] == "Two"
    out = capsys.readouterr().out
    assert f"{old.cookie_path} → {new.cookie_path}" in out
    assert "Verified 5 file(s): session for Test Library (2 cookies), 2 borrow records" in out
    assert SESSION_VALUE not in out


def test_non_interactive_without_yes_copies_nothing(old, new, capsys):
    assert _run(old, new) == 1
    assert not new.data_dir.exists()
    assert "--yes" in capsys.readouterr().err


def test_conflicting_destination_refuses_everything(old, new, capsys):
    new.cookie_path.parent.mkdir(parents=True)
    new.cookie_path.write_text("[]")  # a different, newer session

    assert _run(old, new, "--yes") == 1

    assert new.cookie_path.read_text() == "[]"
    assert not new.borrows_path.exists()  # nothing else copied either
    assert str(new.cookie_path) in capsys.readouterr().err


def test_identical_destination_is_accepted_and_can_clean_up(old, new, capsys):
    assert _run(old, new, "--yes") == 0

    assert _run(old, new, "--yes", "--remove-old") == 0

    assert "identical copies" in capsys.readouterr().out
    assert not old.data_dir.exists()
    assert BorrowStore(new.borrows_path).get("TestLib", "b1")["title"] == "One"


def test_failed_verification_never_offers_cleanup(old, new, monkeypatch, capsys):
    def corrupt_copy(item):
        item.destination.parent.mkdir(parents=True, exist_ok=True)
        item.destination.write_bytes(b"corrupted")

    monkeypatch.setattr(migrate, "_copy", corrupt_copy)

    assert _run(old, new, "--yes", "--remove-old") == 1

    assert old.cookie_path.exists()
    assert "Verification failed" in capsys.readouterr().err


def test_same_source_and_destination_is_rejected(old):
    assert migrate.run(["--from", str(old.data_dir), "--data-dir", str(old.data_dir)]) == 2
