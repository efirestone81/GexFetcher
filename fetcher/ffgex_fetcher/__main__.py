"""
Orchestrator — ties together fetch, parse, compute, map, and POST.

Run with: python -m ffgex_fetcher

Environment variables:
  WORKER_URL       — Cloudflare Worker base URL (required for POST)
  WORKER_SECRET    — Shared secret for /update endpoint (required for POST)
  RISK_FREE_RATE   — Override SOFR (default 0.043)
  TICKERS          — Comma-separated subset (default: all DEFAULT_TICKERS)
  MAX_DTE          — Max days-to-expiry filter (default 30)
  DRY_RUN          — If set, skip the POST and write payload to stdout
  LOG_LEVEL        — DEBUG | INFO | WARNING | ERROR (default INFO)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

from ffgex_fetcher.chain_fetcher import fetch_all_chains
from ffgex_fetcher.gex_engine import (
    parse_chain,
    compute_gex_by_strike,
    compute_oi_by_strike,
    find_gamma_flip,
    identify_walls_clusters,
)
from ffgex_fetcher.futures_mapper import (
    DEFAULT_TICKERS,
    compute_multiplier,
    compute_carry_basis,
)
from ffgex_fetcher.output import build_payload, build_ticker_payload, post_to_worker


log = logging.getLogger("ffgex_fetcher")


def _configure_logging():
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
        stream=sys.stdout,
    )
    logging.Formatter.converter = lambda *_: datetime.now(timezone.utc).timetuple()


def _validate_levels(ticker: str, spot: float, walls: dict, flip: float | None) -> list[str]:
    """Return a list of human-readable warning strings if levels look off."""
    warnings: list[str] = []

    if walls["call_wall"] is None:
        warnings.append(f"{ticker}: no positive GEX found (no call wall)")
    if walls["put_wall"] is None:
        warnings.append(f"{ticker}: no negative GEX found (no put wall)")
    if walls["call_wall"] and walls["put_wall"]:
        if walls["call_wall"]["strike"] == walls["put_wall"]["strike"]:
            warnings.append(f"{ticker}: call wall and put wall at same strike — chain may be degenerate")

    if flip is None:
        warnings.append(f"{ticker}: no gamma flip in sweep range")
    elif abs(flip - spot) / spot > 0.05:
        warnings.append(
            f"{ticker}: flip {flip:.2f} is >5% from spot {spot:.2f}"
        )

    return warnings


async def process_ticker(
    ticker: str,
    chain_or_exc: dict | Exception,
    risk_free_rate: float,
    max_dte: int,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Run the full per-ticker pipeline. Always returns a dict (status=ok or error)."""
    cfg = DEFAULT_TICKERS.get(ticker)
    if cfg is None:
        return {"status": "error", "warnings": [f"{ticker}: no configured TickerConfig"]}

    if isinstance(chain_or_exc, Exception):
        return {"status": "error", "warnings": [f"{ticker}: fetch failed: {chain_or_exc}"]}

    try:
        spot, contracts = parse_chain(chain_or_exc, now_utc=now_utc, max_dte=max_dte)
    except ValueError as e:
        return {"status": "error", "warnings": [f"{ticker}: parse failed: {e}"]}

    if not contracts:
        return {
            "status": "error",
            "warnings": [f"{ticker}: chain had {len(chain_or_exc.get('data',{}).get('options',[]))} raw options, "
                         f"0 survived filtering — possibly stale, all 0DTE, or bad IV/OI data"],
        }

    gex_map = compute_gex_by_strike(spot, contracts, r=risk_free_rate, q=cfg.dividend_yield)
    oi_map = compute_oi_by_strike(contracts)
    walls = identify_walls_clusters(gex_map, oi_map, top_n_clusters=5, top_n_oi=5)
    flip = find_gamma_flip(spot, contracts, r=risk_free_rate, q=cfg.dividend_yield)

    multiplier, mult_warnings = compute_multiplier(spot, cfg)
    # Use a representative 1-month T for carry basis; production can refresh.
    basis_carry = compute_carry_basis(spot, risk_free_rate, cfg, T_years=0.083)

    expiries_set = sorted({c.expiry_utc.date().isoformat() for c in contracts})
    sanity_warnings = _validate_levels(ticker, spot, walls, flip)

    log.info(
        "%s spot=%.2f mult=%.4f CW=%s PW=%s flip=%s contracts=%d",
        ticker, spot, multiplier,
        f"{walls['call_wall']['strike']:.2f}" if walls['call_wall'] else "—",
        f"{walls['put_wall']['strike']:.2f}" if walls['put_wall'] else "—",
        f"{flip:.2f}" if flip else "—",
        len(contracts),
    )

    return build_ticker_payload(
        status="ok",
        spot=spot,
        cfg=cfg,
        multiplier=multiplier,
        basis_carry=basis_carry,
        walls=walls,
        flip=flip,
        contract_count=len(contracts),
        expiries_included=expiries_set,
        warnings=mult_warnings + sanity_warnings,
    )


async def main_async() -> int:
    _configure_logging()

    worker_url = os.environ.get("WORKER_URL")
    worker_secret = os.environ.get("WORKER_SECRET")
    dry_run = bool(os.environ.get("DRY_RUN"))
    risk_free_rate = float(os.environ.get("RISK_FREE_RATE", "0.043"))
    max_dte = int(os.environ.get("MAX_DTE", "30"))

    ticker_csv = os.environ.get("TICKERS")
    tickers = (
        [t.strip().upper() for t in ticker_csv.split(",")]
        if ticker_csv else list(DEFAULT_TICKERS.keys())
    )

    if not dry_run:
        if not worker_url or not worker_secret:
            log.error("WORKER_URL and WORKER_SECRET required (or set DRY_RUN=1)")
            return 2

    log.info("Fetching %s", ", ".join(tickers))
    chains = await fetch_all_chains(tickers)

    per_ticker: dict[str, dict] = {}
    for t in tickers:
        per_ticker[t] = await process_ticker(t, chains.get(t), risk_free_rate, max_dte)

    # If every ticker errored, fail loudly — do NOT overwrite Worker state.
    ok_count = sum(1 for v in per_ticker.values() if v.get("status") == "ok")
    if ok_count == 0:
        log.error("All %d tickers failed; aborting POST to preserve last good payload", len(tickers))
        return 1

    fetch_run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    payload = build_payload(per_ticker, risk_free_rate=risk_free_rate, fetch_run_id=fetch_run_id)
    log.info("Built payload: %d/%d tickers ok, run=%s", ok_count, len(tickers), fetch_run_id)

    if dry_run:
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    await post_to_worker(payload, worker_url=worker_url, worker_secret=worker_secret)
    log.info("Done.")
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
