"""Analyze the captures from probe_auth.py and report what we learned.

Reads:
    scratch/auth-capture/network-pre-login.json   (initial bounce)
    scratch/auth-capture/network-auth.json        (everything during auth)
    scratch/auth-capture/state-post-login.json    (cookies + storage after auth)
    scratch/auth-capture/network-reader.json      (reader-load traffic)
    scratch/auth-capture/state-reader.json        (state inside the reader)

Reports:
    - Cookie summary: which cookies are session-bearing (long expiry, httponly).
    - localStorage / sessionStorage entries that look like tokens.
    - JSON / XHR endpoints called on yourcloudlibrary domains during reader
      load — these are the candidates for httpx replay.
    - Any Authorization / X-Auth-Token / Bearer headers seen on YCL traffic.
    - Distinct host : path patterns for content-fetching requests.
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

CAPTURE_DIR = Path("scratch/auth-capture")


def _load(name: str) -> dict | list | None:
    path = CAPTURE_DIR / name
    if not path.exists():
        print(f"  (missing: {name})")
        return None
    return json.loads(path.read_text())


def _is_token_like(value: str) -> bool:
    if not isinstance(value, str) or len(value) < 20:
        return False
    # JWT shape, hex, base64-ish.
    return bool(re.match(r"^[A-Za-z0-9_\-./+=]{20,}\.?[A-Za-z0-9_\-./+=]*$", value))


def report_cookies(state: dict | None) -> None:
    print("\n=== COOKIES ===")
    if not state:
        return
    cookies = state.get("cookies", [])
    print(f"  total: {len(cookies)}")
    by_domain = defaultdict(list)
    for c in cookies:
        by_domain[c.get("domain", "?")].append(c)
    for domain, entries in sorted(by_domain.items()):
        print(f"\n  {domain} ({len(entries)} cookies):")
        for c in entries:
            flags = []
            if c.get("httpOnly"):
                flags.append("httponly")
            if c.get("secure"):
                flags.append("secure")
            if c.get("sameSite"):
                flags.append(f"sameSite={c['sameSite']}")
            exp = c.get("expires", -1)
            if exp and exp > 0:
                flags.append(f"expires_in={(exp - __import__('time').time()) / 86400:.1f}d")
            else:
                flags.append("session")
            looks_token = _is_token_like(str(c.get("value", "")))
            marker = "  <-- token-like" if looks_token else ""
            print(
                f"    {c['name']!r:32s} = "
                f"{str(c.get('value', ''))[:60]!r:65s} [{','.join(flags)}]{marker}"
            )


def report_storage(state: dict | None) -> None:
    print("\n=== localStorage / sessionStorage ===")
    if not state:
        return
    for key in ("localStorage", "sessionStorage"):
        items = state.get(key, {}) or {}
        print(f"\n  {key}: {len(items)} entries")
        for k, v in items.items():
            looks_token = _is_token_like(str(v))
            marker = "  <-- token-like" if looks_token else ""
            print(f"    {k!r:40s} = {str(v)[:80]!r}{marker}")


def report_endpoints(network: list | None, label: str) -> None:
    print(f"\n=== ENDPOINTS ({label}) ===")
    if not network:
        return
    yclparts = defaultdict(list)
    for entry in network:
        if entry.get("phase") != "request":
            continue
        url = entry.get("url", "")
        host = urlparse(url).hostname or ""
        if "yourcloudlibrary.com" not in host:
            continue
        rt = entry.get("resource_type", "?")
        if rt not in ("xhr", "fetch", "document", "websocket"):
            continue
        path = urlparse(url).path
        # Collapse numeric-ish path components for grouping.
        norm = re.sub(r"/[A-Za-z0-9_-]{8,}", "/{id}", path)
        yclparts[(host, rt, norm)].append(url)
    for (host, rt, path), urls in sorted(yclparts.items()):
        print(f"  [{rt:9s}] {host}{path}    ({len(urls)}x)")
        for u in urls[:2]:
            print(f"      e.g. {u}")


def report_auth_headers(network: list | None, label: str) -> None:
    print(f"\n=== AUTH HEADERS ({label}) ===")
    if not network:
        return
    seen: dict[str, str] = {}
    for entry in network:
        if entry.get("phase") != "request":
            continue
        if "yourcloudlibrary.com" not in (entry.get("url") or ""):
            continue
        for hname, hval in (entry.get("headers") or {}).items():
            lname = hname.lower()
            if lname in (
                "authorization",
                "x-auth-token",
                "x-api-key",
                "x-access-token",
                "x-csrf-token",
                "bearer",
            ):
                key = f"{lname}={hval[:40]}..."
                if key not in seen:
                    seen[key] = entry["url"]
                    print(f"  {hname}: {hval[:80]!r}  on {entry['url'][:90]}")
    if not seen:
        print("  (no auth-style headers found)")


def report_response_bodies(network: list | None, label: str, *, limit: int = 5) -> None:
    print(f"\n=== JSON RESPONSES ({label}) — first {limit} ===")
    if not network:
        return
    seen = 0
    for entry in network:
        if entry.get("phase") != "response":
            continue
        url = entry.get("url") or ""
        if "yourcloudlibrary.com" not in url:
            continue
        ct = (entry.get("headers") or {}).get("content-type", "")
        if "json" not in ct.lower():
            continue
        body = entry.get("body")
        if not body:
            continue
        print(f"\n  {entry.get('status')} {url}")
        try:
            parsed = json.loads(body)
            print(f"    keys: {list(parsed.keys()) if isinstance(parsed, dict) else type(parsed).__name__}")
            print(f"    body[:400]: {body[:400]}")
        except Exception:
            print(f"    body[:300]: {body[:300]}")
        seen += 1
        if seen >= limit:
            break


def main() -> None:
    if not CAPTURE_DIR.exists():
        print(f"No capture at {CAPTURE_DIR}. Run probe_auth.py first.", file=sys.stderr)
        sys.exit(1)

    print("=" * 70)
    print(f"YCL CAPTURE ANALYSIS: {CAPTURE_DIR}")
    print("=" * 70)

    auth_state = _load("state-post-login.json")
    auth_net = _load("network-auth.json")
    reader_state = _load("state-reader.json")
    reader_net = _load("network-reader.json")

    report_cookies(auth_state)
    report_storage(auth_state)

    if reader_net:
        report_endpoints(reader_net, "reader load")
        report_auth_headers(reader_net, "reader load")
        report_response_bodies(reader_net, "reader load", limit=8)
    else:
        report_endpoints(auth_net, "auth phase")
        report_auth_headers(auth_net, "auth phase")
        report_response_bodies(auth_net, "auth phase", limit=8)

    print()
    print("=" * 70)
    print("VERDICT (manual): look for either")
    print("  (a) a stable bearer/session cookie + plain JSON content endpoints")
    print("      → swap Playwright for httpx")
    print("  (b) DRM blob endpoints (.lcpl, .acsm, manifest.json + AES) ")
    print("      → keep Playwright, content is encrypted at the wire")
    print("=" * 70)


if __name__ == "__main__":
    main()
