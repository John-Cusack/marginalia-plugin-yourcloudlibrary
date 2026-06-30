"""One-shot live probe: nail down the catalog-search loader param + result shape.

Confirmed route id: ``routes/library.$name.search`` on the ebook host. This pass
hunts for the param name that actually populates ``results`` and dumps the result
object shape so we know which fields map to {title, author, book_id, available}.

Run: uv run python -m scripts.probe_search
"""

from __future__ import annotations

import asyncio
import json

import httpx

from ycl._paths import COOKIE_PATH
from ycl.api.client import EBOOK_HOST, EPUB_ORIGIN
from ycl.api.cookies import cookies_to_jar, decode_config_cookie
from ycl.session.cookies import CookieStore

QUERY = "harry potter"
ROUTE = "routes/library.$name.search"


async def _probe(client: httpx.AsyncClient, url: str, params: dict) -> None:
    resp = await client.get(url, params=params)
    body = resp.text
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        print(f"[{resp.status_code}] params={params} -> non-JSON len={len(body)}")
        return
    results = parsed.get("results")
    seg = parsed.get("segment")
    n = len(results) if isinstance(results, (dict, list)) else "?"
    print(f"[{resp.status_code}] params={ {k: v for k, v in params.items() if k != '_data'} } "
          f"segment={seg} results_type={type(results).__name__} results_len={n}")
    if results:
        print("    RESULTS SAMPLE:")
        sample = json.dumps(results, indent=2)
        print("\n".join("    " + ln for ln in sample.splitlines()[:80]))


async def main() -> None:
    cookies = CookieStore(COOKIE_PATH).load()
    lib = decode_config_cookie(cookies)
    jar = cookies_to_jar(cookies)
    slug = lib.url_name
    base = f"{EBOOK_HOST}/library/{slug}/search"
    print(f"library: {lib.name}  slug={slug}\n")

    async with httpx.AsyncClient(
        cookies=jar,
        follow_redirects=True,
        timeout=30.0,
        headers={
            "Origin": EPUB_ORIGIN,
            "Referer": f"{EPUB_ORIGIN}/",
            "Accept": "application/json, text/plain, */*",
        },
    ) as client:
        for pname in ["query", "q", "term", "searchTerm", "text", "title", "keyword", "search"]:
            await _probe(client, base, {pname: QUERY, "_data": ROUTE})
        # query + common required companions
        for extra in [{"segment": "eBook"}, {"format": "eBook"}, {"mediaType": "ebook"},
                      {"page": "1"}, {"sortBy": "relevance"}]:
            params = {"query": QUERY, "_data": ROUTE, **extra}
            await _probe(client, base, params)


if __name__ == "__main__":
    asyncio.run(main())
