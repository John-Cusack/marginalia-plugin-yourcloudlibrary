"""Probe the YCL active-loans endpoint with the saved cookies.

Usage:
    uv run python scripts/probe_loans.py

Confirmed live (2026-06-29, Palm Beach County Library System) by injecting
the saved session cookies into a real browser and watching the My-Books
page traffic:

    POST https://ebook.yourcloudlibrary.com/library/{slug}/mybooks/current
         ?segment=1&pageSize=20&_data=routes/library.$name.mybooks.current
    Content-Type: application/x-www-form-urlencoded
    body: format=&sort=BorrowedDateDescending

    -> {"patronItems": [ {itemId, loanId, title, mediaType, dueDate,
                          author, canRenew, canReturn, isSaved}, ... ],
        "totalSegments": N, "currentSegment": 1, "itemsPerSegment": 20,
        "totalItems": M, "RPC_DOMAIN_PUBLIC": "...", "reaktor": "..."}

NOTE: this is a Remix *action* (POST), NOT a `_data` loader GET. The plain
`?_data=routes/library.$name.mybooks` GET returns 400 (x-remix-error) because
the mybooks route itself has no server loader — the loans are produced by
the `.current` child route's action. `dueDate` is the real loan expiry.

This script just replays that request and prints the parsed loans.
"""

from __future__ import annotations

import asyncio
import json

from ycl.api.client import YclClient


async def main() -> None:
    client = YclClient.from_cookie_store()
    try:
        loans = await client.get_loans()
    finally:
        await client.close()
    print(f"library: {client.library.name!r}  active loans: {len(loans)}")
    for loan in loans:
        print(
            json.dumps(
                {
                    "item_id": loan.item_id,
                    "loan_id": loan.loan_id,
                    "title": loan.title,
                    "author": loan.author,
                    "media_type": loan.media_type,
                    "due_date": loan.due_date,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
