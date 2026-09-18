"""``research-engine-ycl-login migrate`` — move pre-0.3.0 plugin data.

Before 0.3.0 the plugin kept its state under ``~/.marginalia/plugins/yourcloudlibrary``.
Core now hands it ``~/.research-engine/plugin-data/yourcloudlibrary`` (or whatever
``PluginContext.data_dir`` resolves to). This command moves the session cookies,
borrow registry, extracted texts, chapter sidecars and partial scrapes across, and
is deliberately conservative:

1. Inventory the old directory and print every source → destination pair.
2. Refuse — copying nothing — if any destination file exists with different bytes.
3. Ask before copying (``--yes`` for non-interactive use).
4. Copy, keeping timestamps and modes; the cookie file is kept owner-only.
5. Verify every copy byte-for-byte, then read the migrated cookies and registry
   through the plugin's own loaders.
6. Only after that, offer to delete the verified originals (``--remove-old``).

Everything stays under the two data directories. Extracted books are licensed
content: this command never writes them anywhere else.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import os
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from .._paths import LEGACY_DATA_DIR, PluginPaths, default_data_dir
from ..api.cookies import decode_config_cookie, redact_secrets
from ..api.errors import LOGIN_COMMAND, NotAuthenticatedError
from ..borrows import BorrowStore
from ..session.cookies import CookieStore

# Lock sidecars and interrupted atomic writes are process state, not data.
_SKIP_NAMES = {"borrows.json.lock"}
_SKIP_PREFIXES = (".borrows-", ".cookies-")

SESSION_FILE_MODE = 0o600


@dataclass(frozen=True)
class PlannedFile:
    kind: str
    source: Path
    destination: Path
    size: int
    state: str  # "copy" | "identical" | "conflict"


def classify(relative: Path) -> str:
    name = relative.name
    if relative == Path("cookies.json"):
        return "session cookies"
    if relative == Path("borrows.json"):
        return "borrow registry"
    if relative.parts[:1] == ("extracted",):
        if name.endswith(".chapters.json"):
            return "chapter sidecar"
        if name.endswith(".partial.txt"):
            return "partial scrape"
        if name.endswith(".txt"):
            return "extracted text"
    return "other"


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def plan(old: Path, new: Path) -> list[PlannedFile]:
    """Every regular file under ``old`` and what copying it to ``new`` would do."""
    planned: list[PlannedFile] = []
    for source in sorted(old.rglob("*")):
        if source.is_symlink() or not source.is_file():
            continue
        if source.name in _SKIP_NAMES or source.name.startswith(_SKIP_PREFIXES):
            continue
        relative = source.relative_to(old)
        destination = new / relative
        if not destination.exists():
            state = "copy"
        elif destination.is_file() and _digest(destination) == _digest(source):
            state = "identical"
        else:
            state = "conflict"
        planned.append(
            PlannedFile(classify(relative), source, destination, source.stat().st_size, state)
        )
    return planned


def _copy(item: PlannedFile) -> str | None:
    """Copy one file atomically. Returns a note when its mode was tightened."""
    item.destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = item.destination.with_name(f".{item.destination.name}.migrating")
    shutil.copy2(item.source, tmp)
    note = None
    if item.kind == "session cookies":
        mode = stat.S_IMODE(item.source.stat().st_mode)
        if mode & ~SESSION_FILE_MODE:
            os.chmod(tmp, mode & SESSION_FILE_MODE)
            note = f"{item.destination.name}: mode {oct(mode)} → {oct(mode & SESSION_FILE_MODE)}"
    os.replace(tmp, item.destination)
    return note


def verify(old: Path, new: Path, planned: list[PlannedFile]) -> list[str]:
    """Problems with the migrated data; empty means it is safe to use."""
    problems = [
        f"{item.destination} does not match {item.source}"
        for item in planned
        if not item.destination.is_file() or _digest(item.destination) != _digest(item.source)
    ]
    new_paths, old_paths = PluginPaths(new), PluginPaths(old)
    if old_paths.cookie_path.is_file():
        cookies = CookieStore(new_paths.cookie_path).load()
        try:
            decode_config_cookie(cookies)
        except (NotAuthenticatedError, ValueError) as exc:
            problems.append(f"migrated cookies are unreadable: {redact_secrets(str(exc))}")
    if old_paths.borrows_path.is_file():
        old_store, new_store = BorrowStore(old_paths.borrows_path), BorrowStore(new_paths.borrows_path)
        libraries = old_store.library_ids()
        if new_store.library_ids() != libraries or any(
            len(new_store.list(lib)) != len(old_store.list(lib)) for lib in libraries
        ):
            problems.append("migrated borrow registry does not match the original")
    return problems


def _summary(new: Path) -> str:
    paths = PluginPaths(new)
    parts = []
    cookies = CookieStore(paths.cookie_path).load()
    if cookies:
        with contextlib.suppress(NotAuthenticatedError, ValueError):
            parts.append(f"session for {decode_config_cookie(cookies).name} ({len(cookies)} cookies)")
    if paths.borrows_path.is_file():
        store = BorrowStore(paths.borrows_path)
        count = sum(len(store.list(lib)) for lib in store.library_ids())
        parts.append(f"{count} borrow records")
    return ", ".join(parts) or "no session or registry"


def _confirm(question: str) -> bool:
    try:
        return input(f"{question} [y/N] ").strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def _remove_verified(old: Path, planned: list[PlannedFile]) -> None:
    for item in planned:
        item.source.unlink(missing_ok=True)
    (old / "borrows.json.lock").unlink(missing_ok=True)
    for directory in sorted((p for p in old.rglob("*") if p.is_dir()), reverse=True):
        with contextlib.suppress(OSError):
            directory.rmdir()  # only succeeds when empty
    with contextlib.suppress(OSError):
        old.rmdir()
    leftover = [p for p in old.rglob("*") if p.is_file()] if old.exists() else []
    if leftover:
        print(f"Kept {len(leftover)} file(s) in {old} that were not part of the migration.")
    else:
        print(f"Removed {old}.")


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog=f"{LOGIN_COMMAND} migrate",
        description="Copy pre-0.3.0 YourCloudLibrary plugin data into the core-managed data directory.",
    )
    parser.add_argument("--from", dest="source", type=Path, default=LEGACY_DATA_DIR,
                        help=f"Old data directory (default: {LEGACY_DATA_DIR}).")
    parser.add_argument("--data-dir", type=Path,
                        help="New plugin data directory (default: the one core passes to the plugin).")
    parser.add_argument("--yes", action="store_true",
                        help="Copy without asking. Does not delete anything by itself.")
    parser.add_argument("--remove-old", action="store_true",
                        help="After a verified copy, delete the migrated originals without asking.")
    args = parser.parse_args(argv)

    old = args.source.expanduser()
    new = (args.data_dir.expanduser() if args.data_dir else default_data_dir())
    if not old.is_dir():
        print(f"Nothing to migrate: {old} does not exist.")
        return 0
    if old.resolve() == new.resolve():
        print(f"Source and destination are the same directory: {old}", file=sys.stderr)
        return 2

    planned = plan(old, new)
    if not planned:
        print(f"Nothing to migrate: {old} has no data files.")
        return 0

    print(f"From: {old}\nTo:   {new}\n")
    for item in planned:
        print(f"  [{item.state:9}] {item.kind:16} {item.source} → {item.destination}")
    by_kind: dict[str, int] = {}
    for item in planned:
        by_kind[item.kind] = by_kind.get(item.kind, 0) + 1
    print("\n" + ", ".join(f"{n} {kind}" for kind, n in sorted(by_kind.items())))

    conflicts = [item for item in planned if item.state == "conflict"]
    if conflicts:
        print(
            f"\nRefusing to migrate: {len(conflicts)} destination file(s) already exist with "
            "different content. Nothing was copied. Resolve these first:",
            file=sys.stderr,
        )
        for item in conflicts:
            print(f"  {item.destination}", file=sys.stderr)
        return 1

    to_copy = [item for item in planned if item.state == "copy"]
    if to_copy:
        if not args.yes:
            if not sys.stdin.isatty():
                print("\nNot a terminal: re-run with --yes to copy.", file=sys.stderr)
                return 1
            if not _confirm(f"\nCopy {len(to_copy)} file(s)?"):
                print("Nothing copied.")
                return 1
        for item in to_copy:
            note = _copy(item)
            if note:
                print(f"Restricted session file permissions ({note}).")
        print(f"Copied {len(to_copy)} file(s).")
    else:
        print("\nThe destination already has identical copies of every file.")

    problems = verify(old, new, planned)
    if problems:
        print("\nVerification failed; the old directory was left untouched:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print(f"Verified {len(planned)} file(s): {_summary(new)}.")

    if args.remove_old or (not args.yes and sys.stdin.isatty()
                           and _confirm(f"Delete the {len(planned)} verified original(s) in {old}?")):
        _remove_verified(old, planned)
    else:
        print(f"Kept the originals in {old}. Remove them later with --remove-old.")
    return 0
