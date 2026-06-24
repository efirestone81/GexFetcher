"""Async CBOE CDN options-chain fetcher."""
from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger(__name__)

CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json"


async def fetch_chain(client, symbol, retries=3):
    url = CBOE_URL.format(symbol=symbol)
    last_err = None
    for attempt in range(retries):
        try:
            resp = await client.get(url)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (404, 403):
                raise RuntimeError(f"{symbol}: HTTP {resp.status_code} (non-retryable)")
            last_err = RuntimeError(f"{symbol}: HTTP {resp.status_code}")
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            last_err = e
        if attempt < retries - 1:
            await asyncio.sleep(1.5 ** attempt)
    raise last_err if last_err else RuntimeError(f"{symbol}: unknown fetch error")


async def fetch_all_chains(symbols, concurrency=4, timeout_s=20.0):
    """Fetch all chains concurrently. Returns {symbol: json | Exception}."""
    results = {}
    sem = asyncio.Semaphore(concurrency)
    headers = {"User-Agent": "FFGEXFetcher/1.1"}

    async with httpx.AsyncClient(timeout=timeout_s, headers=headers) as client:
        async def one(sym):
            async with sem:
                try:
                    results[sym] = await fetch_chain(client, sym)
                except Exception as e:
                    log.warning("Fetch failed for %s: %s", sym, e)
                    results[sym] = e
        await asyncio.gather(*(one(s) for s in symbols))
    return results
