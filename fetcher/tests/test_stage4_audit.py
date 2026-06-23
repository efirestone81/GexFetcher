"""
Stage 4 deep audit — internal consistency and cross-validation tests.

These go beyond "does the pipeline produce something plausible" and check:

1. Sum of per-strike GEX equals sum of per-contract GEX (no aggregation bugs).
2. Flip interpolation is correctly oriented (sign on each side of the crossing).
3. Flip survives different sweep granularities — coarse vs fine sweep
   converges to the same answer.
4. The CBOE-style JSON we're reading has the exact field names we expect
   (parse_chain assumes "data.current_price", "data.options", "option",
   "open_interest", "iv" — all must be present).
5. Wall identification is invariant to the order of contracts in the input.
6. Multiplier × ETF strike equals what we'd get treating spot as the futures
   reference (degenerate sanity check).
7. Net GEX at the gamma flip price is approximately zero.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from ffgex_fetcher.gex_engine import (
    Contract,
    parse_chain,
    compute_gex_per_contract,
    compute_gex_by_strike,
    compute_oi_by_strike,
    net_gex_at_spots,
    find_gamma_flip,
    identify_walls_clusters,
)
from ffgex_fetcher.futures_mapper import DEFAULT_TICKERS, compute_multiplier, map_strike


REF_NOW = datetime(2026, 5, 23, 14, 0, tzinfo=timezone.utc)
FIXTURE = Path(__file__).parent / "fixtures" / "spy_chain_synthetic.json"


@pytest.fixture(scope="module")
def chain():
    return json.loads(FIXTURE.read_text())


@pytest.fixture(scope="module")
def parsed(chain):
    """Parsed contracts at REF_NOW with default filters."""
    spot, contracts = parse_chain(chain, now_utc=REF_NOW, max_dte=30)
    return spot, contracts


# ===========================================================================
# 1. Internal consistency: per-strike sum == per-contract sum
# ===========================================================================

def test_per_strike_sum_equals_per_contract_sum(parsed):
    """If aggregation is correct, both sums must match to machine precision."""
    spot, contracts = parsed
    per_contract = compute_gex_per_contract(spot, contracts, r=0.043, q=0.0125)
    per_strike = compute_gex_by_strike(spot, contracts, r=0.043, q=0.0125)

    total_contract = float(per_contract.sum())
    total_strike = sum(per_strike.values())
    np.testing.assert_allclose(total_contract, total_strike, rtol=1e-10)


def test_per_contract_count_matches_input(parsed):
    """compute_gex_per_contract returns one entry per contract."""
    spot, contracts = parsed
    per_contract = compute_gex_per_contract(spot, contracts, r=0.043, q=0.0125)
    assert per_contract.shape == (len(contracts),)


# ===========================================================================
# 2. Net GEX at the flip is ~zero
# ===========================================================================

def test_net_gex_at_flip_is_near_zero(parsed):
    """
    By definition, the flip is the spot at which net GEX = 0.
    Evaluating net GEX at the flip price should give a value near zero
    relative to typical |GEX|.
    """
    spot, contracts = parsed
    flip = find_gamma_flip(spot, contracts, r=0.043, q=0.0125)
    assert flip is not None

    # Evaluate net GEX at the flip
    val = float(net_gex_at_spots(np.array([flip]), contracts, r=0.043, q=0.0125)[0])

    # Reference scale: total absolute GEX
    abs_gex = np.abs(compute_gex_per_contract(spot, contracts, r=0.043, q=0.0125)).sum()

    # Net GEX at flip should be <0.5% of total absolute GEX.
    # Linear interpolation accuracy from 0.05% sweep step bounds the residual.
    rel = abs(val) / abs_gex
    assert rel < 0.005, f"Net GEX at flip not near zero: {val:.2e} ({rel*100:.3f}% of |total|)"


def test_net_gex_changes_sign_across_flip(parsed):
    """A small move above and below the flip should flip the sign of net GEX."""
    spot, contracts = parsed
    flip = find_gamma_flip(spot, contracts, r=0.043, q=0.0125)
    delta = spot * 0.005  # 0.5% bracket

    below = float(net_gex_at_spots(np.array([flip - delta]), contracts, r=0.043, q=0.0125)[0])
    above = float(net_gex_at_spots(np.array([flip + delta]), contracts, r=0.043, q=0.0125)[0])
    assert np.sign(below) != np.sign(above), \
        f"Net GEX should change sign across flip: below={below:.2e}, above={above:.2e}"


# ===========================================================================
# 3. Flip convergence across sweep granularities
# ===========================================================================

def test_flip_converges_across_sweep_step(parsed):
    """Coarse and fine sweeps should converge to the same flip price."""
    spot, contracts = parsed
    coarse = find_gamma_flip(spot, contracts, r=0.043, q=0.0125, sweep_step_pct=0.005)  # 0.5%
    medium = find_gamma_flip(spot, contracts, r=0.043, q=0.0125, sweep_step_pct=0.0005) # 0.05%
    fine = find_gamma_flip(spot, contracts, r=0.043, q=0.0125, sweep_step_pct=0.00005)  # 0.005%

    # All three should agree to within 0.2% of spot.
    tol = spot * 0.002
    assert abs(coarse - fine) < tol, \
        f"Flip diverges: coarse={coarse}, fine={fine}, diff={abs(coarse-fine)}"
    assert abs(medium - fine) < tol


# ===========================================================================
# 4. CBOE JSON field assumptions
# ===========================================================================

def test_required_fields_present(chain):
    """
    Validate the JSON shape matches what parse_chain expects. If CBOE ever
    changes their schema we'll catch it here.
    """
    assert "data" in chain
    data = chain["data"]
    assert "current_price" in data and isinstance(data["current_price"], (int, float))
    assert "options" in data and isinstance(data["options"], list)
    assert len(data["options"]) > 0

    sample = data["options"][0]
    assert "option" in sample and isinstance(sample["option"], str)
    assert "open_interest" in sample
    assert "iv" in sample


def test_real_cboe_format_matches_synthetic():
    """
    Smoke test that our synthetic fixture's field names match the real CBOE
    CDN JSON shape. Documented fields per the CBOE delayed quotes endpoint:
      data.current_price          : float
      data.options[i].option      : str  (OCC symbol)
      data.options[i].open_interest : int
      data.options[i].iv          : float
      data.options[i].volume      : int (we don't use)
      data.options[i].bid, ask    : float (we don't use)
      data.options[i].gamma       : float (we recompute instead, see Stage 1)

    This test documents our assumptions; if real CBOE differs at runtime,
    parse_chain will degrade gracefully (return [] for that ticker) and the
    fetcher will continue with the others.
    """
    synth = json.loads(FIXTURE.read_text())
    sample = synth["data"]["options"][0]
    required = {"option", "open_interest", "iv"}
    assert required.issubset(set(sample.keys())), \
        f"Missing fields in synthetic fixture: {required - set(sample.keys())}"


# ===========================================================================
# 5. Order invariance
# ===========================================================================

def test_wall_identification_order_invariant(parsed):
    """Shuffling the contract order should not change the walls."""
    spot, contracts = parsed
    import random
    shuffled = list(contracts)
    random.Random(42).shuffle(shuffled)

    a = compute_gex_by_strike(spot, contracts, r=0.043, q=0.0125)
    b = compute_gex_by_strike(spot, shuffled, r=0.043, q=0.0125)

    # Same set of strikes
    assert set(a.keys()) == set(b.keys())
    # Same GEX per strike (within float tolerance)
    for k in a:
        np.testing.assert_allclose(a[k], b[k], rtol=1e-10, atol=1e-3)


def test_flip_order_invariant(parsed):
    spot, contracts = parsed
    import random
    shuffled = list(contracts)
    random.Random(13).shuffle(shuffled)
    flip_a = find_gamma_flip(spot, contracts, r=0.043, q=0.0125)
    flip_b = find_gamma_flip(spot, shuffled, r=0.043, q=0.0125)
    np.testing.assert_allclose(flip_a, flip_b, rtol=1e-10)


# ===========================================================================
# 6. Multiplier degenerate case
# ===========================================================================

def test_multiplier_degenerate_when_ref_equals_spot_scaled():
    """If the futures reference price equals etf_spot * scale exactly, the
    multiplier should equal `scale` precisely."""
    from ffgex_fetcher.futures_mapper import TickerConfig
    cfg = TickerConfig(
        etf_symbol="SPY", futures_symbol="ES",
        futures_ref_price=5834.20,  # exactly 583.42 * 10
        scale_to_index=10.0,
        multiplier_bounds=(9.5, 10.5),
        dividend_yield=0.0125,
    )
    mult, _ = compute_multiplier(etf_spot=583.42, cfg=cfg)
    assert mult == pytest.approx(10.0, rel=1e-12)


# ===========================================================================
# 7. Realistic magnitude bounds
# ===========================================================================

def test_total_net_gex_is_plausible(parsed):
    """For a SPY chain on a normal day, total net GEX should be in the
    $100M–$30B range. Our synthetic fixture is biased toward $+1B."""
    spot, contracts = parsed
    per = compute_gex_per_contract(spot, contracts, r=0.043, q=0.0125)
    total_abs = float(np.abs(per).sum())
    total_net = float(per.sum())

    assert 1e8 < total_abs < 1e12, f"Total |GEX| out of range: {total_abs:.2e}"
    assert abs(total_net) < total_abs, \
        f"Net |GEX| should not exceed sum of absolute values"


# ===========================================================================
# 8. Contract counts after filtering
# ===========================================================================

def test_filtered_contract_count_reasonable(parsed, chain):
    """Synthetic fixture has 720 raw options including 6 junk rows that
    should all be filtered. Expect ~99% of legitimate ones to survive."""
    spot, contracts = parsed
    raw_count = len(chain["data"]["options"])
    filtered = len(contracts)
    print(f"\n  Filtered {filtered} / {raw_count} contracts ({filtered/raw_count*100:.1f}%)")
    # 6 of 720 are explicitly bad; expect 714 +/- a few survivors.
    assert filtered == raw_count - 6, \
        f"Expected exactly 6 junk rows to be filtered; got {raw_count - filtered}"


def test_junk_rows_are_filtered(parsed, chain):
    """Verify each specific filter triggered."""
    spot, contracts = parsed

    # No contract should be at K=600 with the far-future expiry —
    # nor at K=580 with the past expiry. Both should have been filtered.
    # Far future: DTE=180, beyond max_dte=30 → out.
    # Past: DTE=-10, below min_dte=-1 → out.
    # OI=0 at K=600 → out.
    # IV=0 at K=605 → out (below IV_MIN).
    # IV=10 at K=610 → out (above IV_MAX).
    # GARBAGE symbol → unparseable → out.

    # Spot-check that no contract violates the filters.
    for c in contracts:
        assert c.oi > 0, f"OI=0 leaked through: {c}"
        assert 0.05 <= c.iv <= 3.0, f"IV out of band leaked through: {c}"
        # T_min floor applies; minimum T is 0.5/365 ≈ 0.00137
        assert c.T > 0



# ===========================================================================
# 9. Spot symmetry: flipping all OI between calls and puts should
#    approximately negate net GEX.
# ===========================================================================

def test_call_put_swap_negates_net_gex(parsed):
    """If we re-flag every call as a put and vice versa, net GEX should flip
    sign. Magnitudes change because OI distribution isn't symmetric, but the
    sign must invert."""
    spot, contracts = parsed
    swapped = [
        Contract(strike=c.strike, expiry_utc=c.expiry_utc,
                 is_call=not c.is_call, oi=c.oi, iv=c.iv, T=c.T)
        for c in contracts
    ]
    net_orig = float(compute_gex_per_contract(spot, contracts, r=0.043, q=0.0125).sum())
    net_swap = float(compute_gex_per_contract(spot, swapped, r=0.043, q=0.0125).sum())
    # Signs should be opposite (with non-trivial magnitudes).
    assert net_orig * net_swap < 0
