"""Async CBOE CDN options-chain fetcher."""
from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger(__name__)

CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json"

DEFAULT_TIMEOUT = 20.0
DEFAULT_HEADERS = {"User-Agent": "FFGEXFetcher/1.1"}


class ChainFetchError(Exception):
    """Raised when a chain cannot be fetched (non-retryable or exhausted retries)."""
    pass


async def fetch_chain(symbol, client, retries=3):
    """Fetch one symbol's chain JSON. Raises ChainFetchError on failure.

    Signature: (symbol, client) — symbol first, then the httpx.AsyncClient.
    """
    url = CBOE_URL.format(symbol=symbol)
    last_err = None
    for attempt in range(retries):
        try:
            resp = await client.get(url)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (404, 403):
                raise ChainFetchError(f"{symbol}: HTTP {resp.status_code} (non-retryable)")
            last_err = ChainFetchError(f"{symbol}: HTTP {resp.status_code}")
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            last_err = e
        if attempt < retries - 1:
            await asyncio.sleep(1.5 ** attempt)
    if isinstance(last_err, ChainFetchError):
        raise last_err
    raise ChainFetchError(f"{symbol}: {last_err}" if last_err else f"{symbol}: unknown fetch error")


async def fetch_all_chains(symbols, concurrency=4, timeout_s=DEFAULT_TIMEOUT):
    """Fetch all chains concurrently. Returns {symbol: json | Exception}."""
    results = {}
    sem = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(timeout=timeout_s, headers=DEFAULT_HEADERS) as client:
        async def one(sym):
            async with sem:
                try:
                    results[sym] = await fetch_chain(sym, client)
                except Exception as e:
                    log.warning("Fetch failed for %s: %s", sym, e)
                    results[sym] = e
        await asyncio.gather(*(one(s) for s in symbols))
    return results