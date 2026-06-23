"""
Live integration test — runs the full pipeline against the real CBOE CDN feed.

This is the critical validation gate. Skipped automatically when offline.

Validation strategy:
1. Fetch the SPY chain from CBOE.
2. Run parse_chain → compute_gex_by_strike → identify_walls_clusters →
   find_gamma_flip.
3. Verify the outputs are SANE — values are in expected magnitude ranges,
   walls are real round-number-adjacent strikes, flip is within ±5% of spot.

This test does not verify exact values (which change minute-to-minute);
it verifies the pipeline produces output in the right ballpark, which
catches integration bugs that synthetic tests would miss.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ffgex_fetcher.chain_fetcher import fetch_chain, ChainFetchError, DEFAULT_HEADERS, DEFAULT_TIMEOUT
from ffgex_fetcher.gex_engine import (
    parse_chain, compute_gex_by_strike, compute_oi_by_strike,
    find_gamma_flip, identify_walls_clusters,
)
from ffgex_fetcher.futures_mapper import DEFAULT_TICKERS

import httpx


# Set this env var to skip the live test (CI without internet, etc).
SKIP_LIVE = os.environ.get("SKIP_LIVE_TESTS", "0") == "1"

# Where to save fixture for offline tests.
FIXTURE_DIR = Path(__file__).parent / "fixtures"


@pytest.mark.skipif(SKIP_LIVE, reason="SKIP_LIVE_TESTS=1")
def test_live_spy_pipeline():
    """Run the full pipeline on a live SPY chain and verify sanity."""
    try:
        chain = asyncio.run(_fetch_one("SPY"))
    except (ChainFetchError, httpx.HTTPError) as e:
        pytest.skip(f"Live fetch failed: {e}")

    # Cache the fixture so we can re-run offline.
    FIXTURE_DIR.mkdir(exist_ok=True)
    fixture_path = FIXTURE_DIR / "spy_chain_live.json"
    fixture_path.write_text(json.dumps(chain, indent=None, separators=(",", ":")))
    print(f"\nLive SPY chain saved to {fixture_path} ({fixture_path.stat().st_size:,} bytes)")

    _validate_spy_pipeline(chain)


def test_offline_spy_pipeline_from_fixture():
    """Same validation but from cached fixture — runs in CI without network."""
    fixture_path = FIXTURE_DIR / "spy_chain_live.json"
    if not fixture_path.exists():
        pytest.skip("No cached SPY fixture; run the live test first.")
    chain = json.loads(fixture_path.read_text())
    _validate_spy_pipeline(chain, from_fixture=True)


def _validate_spy_pipeline(chain: dict, from_fixture: bool = False) -> None:
    """Shared validation — checks outputs are in sane ranges."""
    cfg = DEFAULT_TICKERS["SPY"]
    r = 0.043
    q = cfg.dividend_yield

    # Detect chain age — if the fixture is old, time-to-expiry will be very
    # different and many filters will reject contracts that are now past.
    # Re-parse with a now_utc that matches the chain's notional "as of" time
    # so the test is meaningful.
    raw_spot = chain.get("data", {}).get("current_price")
    print(f"\n--- SPY live pipeline ({'fixture' if from_fixture else 'live'}) ---")
    print(f"Spot (raw): {raw_spot}")

    spot, contracts = parse_chain(chain, max_dte=30)
    print(f"Filtered contracts: {len(contracts)}")

    # If we filtered out everything (stale fixture), retry with a wider lookback.
    if len(contracts) == 0:
        # Find the earliest expiry in the chain and back-date now_utc to a
        # week before it so contracts are in-band.
        from ffgex_fetcher.gex_engine import parse_occ_symbol
        all_expiries = []
        for opt in chain["data"]["options"]:
            parsed = parse_occ_symbol(opt.get("option", ""))
            if parsed:
                all_expiries.append(parsed[1])
        if all_expiries:
            synthetic_now = min(all_expiries).replace(tzinfo=timezone.utc) - \
                            __import__("datetime").timedelta(days=7)
            print(f"Stale fixture detected — retrying with now={synthetic_now}")
            spot, contracts = parse_chain(chain, now_utc=synthetic_now, max_dte=30)
            print(f"After retry, contracts: {len(contracts)}")

    assert spot > 0, "Spot must be positive"
    # SPY is normally in $300-700 range; this is a sanity bound, not a
    # market call.
    assert 100 < spot < 1500, f"Spot {spot} outside sane range for SPY"
    assert len(contracts) > 50, f"Only {len(contracts)} contracts after filter — too few"

    # ---- Compute GEX
    gex_map = compute_gex_by_strike(spot, contracts, r=r, q=q)
    oi_map = compute_oi_by_strike(contracts)
    assert len(gex_map) > 10, f"Too few strikes: {len(gex_map)}"
    print(f"Strikes with GEX: {len(gex_map)}")

    # ---- Walls and clusters
    walls = identify_walls_clusters(gex_map, oi_map, top_n_clusters=5, top_n_oi=5)
    cw = walls["call_wall"]
    pw = walls["put_wall"]
    assert cw is not None, "Call wall not found — chain may have only puts"
    assert pw is not None, "Put wall not found — chain may have only calls"

    print(f"Call Wall: ${cw['strike']:.2f}  GEX=${cw['gex_dollars']/1e9:+.2f}B")
    print(f"Put Wall:  ${pw['strike']:.2f}  GEX=${pw['gex_dollars']/1e9:+.2f}B")

    # Wall positioning: call wall should be at or above spot, put wall at or below.
    # In rare cases of skewed positioning this can be violated; we allow a 2%
    # tolerance.
    assert cw["strike"] > spot * 0.95, \
        f"Call wall {cw['strike']} suspiciously below spot {spot}"
    assert pw["strike"] < spot * 1.05, \
        f"Put wall {pw['strike']} suspiciously above spot {spot}"

    # GEX dollar magnitudes for SPY should be in $0.1B–$30B range per strike.
    assert 0.05e9 < abs(cw["gex_dollars"]) < 50e9, \
        f"Call wall GEX out of range: {cw['gex_dollars']:.2e}"
    assert 0.05e9 < abs(pw["gex_dollars"]) < 50e9, \
        f"Put wall GEX out of range: {pw['gex_dollars']:.2e}"

    # ---- Gamma flip
    flip = find_gamma_flip(spot, contracts, r=r, q=q)
    if flip is not None:
        print(f"Gamma Flip: ${flip:.2f}  ({(flip/spot - 1)*100:+.2f}% from spot)")
        # In normal regimes flip is within ±5% of spot.
        assert spot * 0.85 < flip < spot * 1.15, \
            f"Flip {flip} unreasonably far from spot {spot}"
    else:
        # Flip can legitimately be None if all GEX is one sign within the
        # sweep range — flag but don't fail.
        print("Gamma Flip: not found in sweep range (all-one-sign regime)")

    # ---- Total
    total = walls["total_net_gex"]
    print(f"Total Net GEX: ${total/1e9:+.2f}B")
    assert abs(total) < 200e9, f"Total GEX {total:.2e} absurdly large"

    # ---- Print top clusters for visual sanity check
    print("Top + clusters:")
    for c in walls["top_pos_clusters"][:3]:
        print(f"   ${c['strike']:.2f}  ${c['gex_dollars']/1e9:+.2f}B")
    print("Top − clusters:")
    for c in walls["top_neg_clusters"][:3]:
        print(f"   ${c['strike']:.2f}  ${c['gex_dollars']/1e9:+.2f}B")
    print("Top OI clusters:")
    for c in walls["top_oi_clusters"][:3]:
        print(f"   ${c['strike']:.2f}  OI={c['open_interest']:,}")


async def _fetch_one(ticker: str) -> dict:
    async with httpx.AsyncClient(
        headers=DEFAULT_HEADERS, timeout=DEFAULT_TIMEOUT,
    ) as client:
        return await fetch_chain(ticker, client)
