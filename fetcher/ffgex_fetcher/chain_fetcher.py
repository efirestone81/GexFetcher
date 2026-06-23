"""
CBOE CDN options chain fetcher.

The CDN endpoint we use:
    https://cdn.cboe.com/api/global/delayed_quotes/options/{TICKER}.json

It returns the full delayed quote snapshot including per-contract Greeks,
IV, OI, and the underlying's current_price. Public, undocumented, no auth.
We use a browser-like User-Agent because some CBOE endpoints 403 without one.

The endpoint should ideally only be hit a handful of times per day; the
production scheduler runs at 09:35, 12:00, 15:55 ET plus an overnight pass.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Iterable

import httpx


log = logging.getLogger(__name__)


CBOE_URL_TEMPLATE = "https://cdn.cboe.com/api/global/delayed_quotes/options/{ticker}.json"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36 FFGEX/1.0"
    ),
    "Accept": "application/json",
}

DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)


class ChainFetchError(RuntimeError):
    """Raised when a chain fetch fails irrecoverably for one ticker."""


async def fetch_chain(
    ticker: str,
    client: httpx.AsyncClient,
    *,
    retries: int = 2,
    retry_backoff: float = 1.5,
) -> dict:
    """
    Fetch one ticker's options chain JSON.

    Retries on transient errors (network, 5xx). Surfaces ChainFetchError on
    final failure. The caller's calling pattern is to catch and continue
    so one bad ticker does not kill the whole run.
    """
    url = CBOE_URL_TEMPLATE.format(ticker=ticker.upper())
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = await client.get(url)
            if resp.status_code == 200:
                return resp.json()
            if 500 <= resp.status_code < 600:
                last_err = ChainFetchError(
                    f"{ticker}: HTTP {resp.status_code} (transient)"
                )
            else:
                # Non-retryable (4xx).
                raise ChainFetchError(
                    f"{ticker}: HTTP {resp.status_code} (non-retryable)"
                )
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            last_err = e
        if attempt < retries:
            await asyncio.sleep(retry_backoff ** attempt)
    raise ChainFetchError(
        f"{ticker}: all {retries + 1} attempts failed; last error: {last_err}"
    )


async def fetch_all_chains(
    tickers: Iterable[str],
    *,
    concurrency: int = 4,
) -> dict[str, dict | Exception]:
    """
    Fetch chains for multiple tickers concurrently.

    Returns a map {ticker: chain_json_or_exception}. The caller decides
    per-ticker whether to proceed or mark as error.
    """
    sem = asyncio.Semaphore(concurrency)
    results: dict[str, dict | Exception] = {}

    async with httpx.AsyncClient(
        headers=DEFAULT_HEADERS, timeout=DEFAULT_TIMEOUT, follow_redirects=True,
    ) as client:
        async def _one(t: str):
            async with sem:
                try:
                    results[t] = await fetch_chain(t, client)
                except Exception as exc:
                    log.warning("Fetch failed for %s: %s", t, exc)
                    results[t] = exc

        await asyncio.gather(*(_one(t) for t in tickers))

    return results
