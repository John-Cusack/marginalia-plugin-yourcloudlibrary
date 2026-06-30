"""P0.1 — retry behaviour of YclClient._get.

Transient failures (timeouts, connection drops, 429, 5xx) are retried with
backoff; auth/not-found failures are not. The backoff sleep is patched out so
these run instantly while still asserting how many sleeps happened and with
what delay (for the Retry-After case).
"""

from __future__ import annotations

import httpx
import pytest

from ycl.api.client import YclClient
from ycl.api.errors import AuthExpiredError, YclApiError
from ycl.api.types import LibraryInfo

LIBRARY = LibraryInfo(
    name="Palm Beach County Library System",
    url_name="PalmBeachCountyLibrarySystem",
    library_uuid="x",
)
JAR = {"__session_PROD": "fake", "__config_PROD": "fake"}
URL = "https://epubservice.yourcloudlibrary.com/content/x/manifest.json"


def _client(handler, **kwargs):
    client = YclClient(cookie_jar=JAR, library_info=LIBRARY, backoff_base=0.0, **kwargs)
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        cookies=JAR,
        follow_redirects=True,
        timeout=5.0,
    )
    # Record sleeps instead of actually sleeping.
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client._sleep = fake_sleep  # type: ignore[method-assign]
    return client, sleeps


async def test_retries_5xx_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="upstream down")
        return httpx.Response(200, text="ok")

    client, sleeps = _client(handler)
    async with client:
        resp = await client._get(URL)
    assert resp.status_code == 200
    assert calls["n"] == 3          # 2 failures + 1 success
    assert len(sleeps) == 2         # one backoff before each retry


async def test_retries_exhausted_raises_api_error():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="still down")

    client, sleeps = _client(handler, max_retries=3)
    with pytest.raises(YclApiError):
        async with client:
            await client._get(URL)
    assert calls["n"] == 4          # initial + 3 retries
    assert len(sleeps) == 3


async def test_retries_on_timeout():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("timed out", request=request)
        return httpx.Response(200, text="ok")

    client, sleeps = _client(handler)
    async with client:
        resp = await client._get(URL)
    assert resp.status_code == 200
    assert calls["n"] == 2
    assert len(sleeps) == 1


async def test_retries_on_connection_error():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, text="ok")

    client, sleeps = _client(handler)
    async with client:
        resp = await client._get(URL)
    assert resp.status_code == 200
    assert calls["n"] == 2


async def test_429_respects_retry_after():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"}, text="slow down")
        return httpx.Response(200, text="ok")

    client, sleeps = _client(handler)
    async with client:
        resp = await client._get(URL)
    assert resp.status_code == 200
    assert sleeps == [7.0]          # honoured the header verbatim


async def test_429_retry_after_is_capped():
    # A pathological Retry-After must not block for the whole hour.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "3600"}, text="slow")

    client, sleeps = _client(handler, retry_after_max=30.0, max_retries=1)
    with pytest.raises(YclApiError):
        async with client:
            await client._get(URL)
    assert sleeps == [30.0]          # capped at retry_after_max, not 3600


async def test_no_retry_on_404():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, text="missing")

    client, sleeps = _client(handler)
    with pytest.raises(YclApiError):
        async with client:
            await client._get(URL)
    assert calls["n"] == 1          # not retried
    assert sleeps == []


async def test_no_retry_on_401():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, text="unauthorized")

    client, sleeps = _client(handler)
    with pytest.raises(AuthExpiredError):
        async with client:
            await client._get(URL)
    assert calls["n"] == 1
    assert sleeps == []
