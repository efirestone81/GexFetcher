"""
Orchestrator — fetch, parse, compute blended + 0DTE levels, map, POST.

Run with: python -m ffgex_fetcher

Key env vars: WORKER_URL, WORKER_SECRET, RISK_FREE_RATE, TICKERS, MAX_DTE,
DRY_RUN, LOG_LEVEL.

For SPX/NDX (index underlyings), the indicator applies LiveBasis at draw time,
but we still ship multiplier + carry mappings for compatibility and for the
ETF tickers.
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
    select_0dte_contracts,
    compute_gex_by_strike,
    compute_oi_by_strike,
    find_gamma_flip,
    identify_walls_clusters,
    compute_expected_move_1d,
)
from ffgex_fetcher.futures_mapper import (
    DEFAULT_TICKERS,
    compute_multiplier,
    compute_carry_basis,
)
from ffgex_fetcher.output import build_payload, build_ticker_payload, post_to_worker

log = logging.getLogger("ffgex_fetcher")

# Approximate days-to-futures-expiry for the carry-basis calc. Index futures
# (ES/NQ) are quarterly; mid-quarter the active contract averages ~45 DTE.
# This only affects the futures_basis mapping, not the levels themselves.
CARRY_T_YEARS = 45.0 / 365.0


def _configure_logging():
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
        stream=sys.stdout,
    )


def _validate(name, spot, walls, flip):
    w = []
    if walls["call_wall"] is None:
        w.append(f"{name}: no positive GEX (no call wall)")
    if walls["put_wall"] is None:
        w.append(f"{name}: no negative GEX (no put wall)")
    if flip is not None and spot and abs(flip - spot) / spot > 0.05:
        w.append(f"{name}: flip {flip:.2f} >5% from spot {spot:.2f}")
    return w


async def process_ticker(ticker, chain_or_exc, risk_free_rate, max_dte, now_utc=None):
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
        return {"status": "error",
                "warnings": [f"{ticker}: 0 contracts survived filtering (stale/empty chain)"]}

    q = cfg.dividend_yield

    # --- Blended levels (all expiries <= max_dte) ---
    blended_gex = compute_gex_by_strike(spot, contracts, r=risk_free_rate, q=q)
    blended_oi = compute_oi_by_strike(contracts)
    blended_walls = identify_walls_clusters(blended_gex, blended_oi, top_n_clusters=5, top_n_oi=5)
    blended_flip = find_gamma_flip(spot, contracts, r=risk_free_rate, q=q)

    # --- 0DTE levels (strict same-day expiry, Option A) ---
    dte0_contracts = select_0dte_contracts(contracts, now_utc=now_utc)
    if dte0_contracts:
        dte0_gex = compute_gex_by_strike(spot, dte0_contracts, r=risk_free_rate, q=q)
        dte0_oi = compute_oi_by_strike(dte0_contracts)
        dte0_walls = identify_walls_clusters(dte0_gex, dte0_oi, top_n_clusters=5, top_n_oi=0)
        dte0_flip = find_gamma_flip(spot, dte0_contracts, r=risk_free_rate, q=q)
    else:
        dte0_walls = {"call_wall": None, "put_wall": None, "top_pos_clusters": [],
                      "top_neg_clusters": [], "top_oi_clusters": [], "total_net_gex": 0.0}
        dte0_flip = None

    # --- 1D expected move (from 0DTE ATM IV if available, else nearest) ---
    em_source = dte0_contracts if dte0_contracts else contracts
    expected_move, atm_iv = compute_expected_move_1d(spot, em_source)

    # --- Futures mapping ---
    multiplier, mult_warnings = compute_multiplier(spot, cfg)
    basis_carry = compute_carry_basis(spot, risk_free_rate, cfg, CARRY_T_YEARS)

    expiries = sorted({c.expiry_utc.date().isoformat() for c in contracts})
    warnings = mult_warnings + _validate(ticker, spot, blended_walls, blended_flip)

    log.info(
        "%s spot=%.2f mult=%.4f | blended CW=%s PW=%s flip=%s | 0DTE n=%d CW=%s PW=%s flip=%s",
        ticker, spot, multiplier,
        _fmt(blended_walls["call_wall"]), _fmt(blended_walls["put_wall"]),
        f"{blended_flip:.2f}" if blended_flip else "—",
        len(dte0_contracts),
        _fmt(dte0_walls["call_wall"]), _fmt(dte0_walls["put_wall"]),
        f"{dte0_flip:.2f}" if dte0_flip else "—",
    )

    return build_ticker_payload(
        status="ok", spot=spot, cfg=cfg, multiplier=multiplier, basis_carry=basis_carry,
        blended_walls=blended_walls, blended_flip=blended_flip,
        dte0_walls=dte0_walls, dte0_flip=dte0_flip, dte0_contract_count=len(dte0_contracts),
        expected_move=expected_move, atm_iv=atm_iv,
        contract_count=len(contracts), expiries_included=expiries, warnings=warnings,
    )


def _fmt(wall):
    return f"{wall['strike']:.2f}" if wall else "—"


async def main_async():
    _configure_logging()
    worker_url = os.environ.get("WORKER_URL")
    worker_secret = os.environ.get("WORKER_SECRET")
    dry_run = bool(os.environ.get("DRY_RUN"))
    risk_free_rate = float(os.environ.get("RISK_FREE_RATE", "0.043"))
    max_dte = int(os.environ.get("MAX_DTE", "30"))
    ticker_csv = os.environ.get("TICKERS")
    tickers = ([t.strip().upper() for t in ticker_csv.split(",")]
               if ticker_csv else list(DEFAULT_TICKERS.keys()))

    if not dry_run and (not worker_url or not worker_secret):
        log.error("WORKER_URL and WORKER_SECRET required (or set DRY_RUN=1)")
        return 2

    # Map ticker key -> CBOE symbol for fetching
    symbols = {DEFAULT_TICKERS[t].symbol: t for t in tickers if t in DEFAULT_TICKERS}
    log.info("Fetching %s", ", ".join(symbols.keys()))
    chains_by_symbol = await fetch_all_chains(list(symbols.keys()))
    # Re-key results back to ticker names
    chains = {symbols[sym]: data for sym, data in chains_by_symbol.items()}

    per_ticker: dict[str, dict] = {}
    for t in tickers:
        per_ticker[t] = await process_ticker(t, chains.get(t), risk_free_rate, max_dte)

    ok = sum(1 for v in per_ticker.values() if v.get("status") == "ok")
    if ok == 0:
        log.error("All %d tickers failed; aborting POST", len(tickers))
        return 1

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    payload = build_payload(per_ticker, risk_free_rate=risk_free_rate, fetch_run_id=run_id)
    log.info("Built payload: %d/%d ok, run=%s", ok, len(tickers), run_id)

    if dry_run:
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    await post_to_worker(payload, worker_url=worker_url, worker_secret=worker_secret)
    log.info("Done.")
    return 0


def main():
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
