"""
Unit tests for gex_engine.py.

Strategy: build synthetic option chains where we know the right answer by
construction, then verify the engine produces it.

Key invariants to test:
1. OCC symbol parsing handles standard and edge-case formats.
2. parse_chain applies all filters correctly (OI, IV, DTE, malformed).
3. Per-strike GEX aggregation matches manual computation.
4. Call GEX is positive, put GEX is negative (sign convention).
5. find_gamma_flip locates zero-crossings to ~0.1% accuracy.
6. Walls and clusters are correctly identified.
7. Gamma sanity filter (GAMMA_MAX) drops anomalous contracts.
8. Empty chain handled gracefully.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from ffgex_fetcher.gex_engine import (
    Contract,
    parse_occ_symbol,
    parse_chain,
    compute_gex_per_contract,
    compute_gex_by_strike,
    compute_oi_by_strike,
    net_gex_at_spots,
    find_gamma_flip,
    identify_walls_clusters,
    CONTRACT_MULTIPLIER,
    GAMMA_MAX,
)
from ffgex_fetcher.greeks import bs_gamma


# A reference "now" for deterministic DTE math.
REF_NOW = datetime(2026, 5, 23, 14, 0, tzinfo=timezone.utc)


# ===========================================================================
# OCC symbol parsing
# ===========================================================================

def test_parse_occ_spy_call():
    result = parse_occ_symbol("SPY240419C00450000")
    assert result is not None
    strike, expiry, is_call = result
    assert strike == 450.000
    assert is_call is True
    assert expiry == datetime(2024, 4, 19, 20, 0, tzinfo=timezone.utc)


def test_parse_occ_qqq_put_fractional_strike():
    result = parse_occ_symbol("QQQ240517P00380500")
    assert result is not None
    strike, expiry, is_call = result
    assert strike == 380.500
    assert is_call is False


def test_parse_occ_spx_index():
    result = parse_occ_symbol("SPX240419C05000000")
    assert result is not None
    strike, _, is_call = result
    assert strike == 5000.000
    assert is_call is True


def test_parse_occ_high_strike():
    """NDX strikes go to 5 digits before decimal."""
    result = parse_occ_symbol("NDX240419C20000000")
    assert result is not None
    strike, _, _ = result
    assert strike == 20000.000


@pytest.mark.parametrize("bad", [
    "",
    "SPY",
    "SPY240419X00450000",        # invalid C/P field
    "SPY2404019C00450000",       # extra digit in date
    "SPY240419C0045000",         # short strike
    "SPY999999C00450000",        # invalid month/day
    "240419C00450000",           # no root
    "spy240419c00450000",        # lowercase
])
def test_parse_occ_malformed_returns_none(bad):
    assert parse_occ_symbol(bad) is None


# ===========================================================================
# parse_chain — filter behavior
# ===========================================================================

def _make_chain(options: list[dict], spot: float = 100.0) -> dict:
    return {"data": {"current_price": spot, "options": options}}


def _occ(root: str, expiry: datetime, is_call: bool, strike: float) -> str:
    yy = expiry.year % 100
    cp = "C" if is_call else "P"
    return f"{root}{yy:02d}{expiry.month:02d}{expiry.day:02d}{cp}{int(strike * 1000):08d}"


def test_parse_chain_basic():
    exp = REF_NOW + timedelta(days=14)
    chain = _make_chain([
        {"option": _occ("SPY", exp, True, 100.0), "open_interest": 500, "iv": 0.20},
        {"option": _occ("SPY", exp, False, 100.0), "open_interest": 400, "iv": 0.22},
    ], spot=100.0)
    spot, contracts = parse_chain(chain, now_utc=REF_NOW)
    assert spot == 100.0
    assert len(contracts) == 2
    assert any(c.is_call for c in contracts)
    assert any(not c.is_call for c in contracts)


def test_parse_chain_filters_zero_oi():
    exp = REF_NOW + timedelta(days=14)
    chain = _make_chain([
        {"option": _occ("SPY", exp, True, 100.0), "open_interest": 0, "iv": 0.20},
        {"option": _occ("SPY", exp, True, 105.0), "open_interest": 100, "iv": 0.20},
    ])
    _, contracts = parse_chain(chain, now_utc=REF_NOW)
    assert len(contracts) == 1
    assert contracts[0].strike == 105.0


def test_parse_chain_filters_bad_iv():
    exp = REF_NOW + timedelta(days=14)
    chain = _make_chain([
        {"option": _occ("SPY", exp, True, 100.0), "open_interest": 100, "iv": 0.01},  # too low
        {"option": _occ("SPY", exp, True, 105.0), "open_interest": 100, "iv": 5.0},   # too high
        {"option": _occ("SPY", exp, True, 110.0), "open_interest": 100, "iv": 0.20},  # ok
    ])
    _, contracts = parse_chain(chain, now_utc=REF_NOW)
    assert len(contracts) == 1
    assert contracts[0].strike == 110.0


def test_parse_chain_filters_dte():
    chain = _make_chain([
        # Past expiry — rejected.
        {"option": _occ("SPY", REF_NOW - timedelta(days=5), True, 100.0),
         "open_interest": 100, "iv": 0.20},
        # Beyond max_dte (default 30) — rejected.
        {"option": _occ("SPY", REF_NOW + timedelta(days=60), True, 100.0),
         "open_interest": 100, "iv": 0.20},
        # In-band — kept.
        {"option": _occ("SPY", REF_NOW + timedelta(days=14), True, 100.0),
         "open_interest": 100, "iv": 0.20},
    ])
    _, contracts = parse_chain(chain, now_utc=REF_NOW)
    assert len(contracts) == 1


def test_parse_chain_allows_0dte():
    # Today's expiry — DTE ≈ +0.25 (expiry at 20:00 UTC, now at 14:00 UTC).
    chain = _make_chain([
        {"option": _occ("SPY", REF_NOW, True, 100.0),
         "open_interest": 1000, "iv": 0.30},
    ])
    _, contracts = parse_chain(chain, now_utc=REF_NOW)
    assert len(contracts) == 1
    # T floor should apply for very-near-expiry — but DTE > 0 here, so T ~ 0.0007.
    assert contracts[0].T > 0


def test_parse_chain_skips_malformed_symbols():
    exp = REF_NOW + timedelta(days=14)
    chain = _make_chain([
        {"option": "GARBAGE", "open_interest": 100, "iv": 0.20},
        {"option": _occ("SPY", exp, True, 100.0), "open_interest": 100, "iv": 0.20},
    ])
    _, contracts = parse_chain(chain, now_utc=REF_NOW)
    assert len(contracts) == 1


def test_parse_chain_rejects_missing_spot():
    with pytest.raises(ValueError, match="current_price"):
        parse_chain({"data": {"current_price": None, "options": []}}, now_utc=REF_NOW)


def test_parse_chain_rejects_naive_now():
    with pytest.raises(ValueError, match="timezone"):
        parse_chain(_make_chain([]), now_utc=datetime(2026, 5, 23, 14, 0))


# ===========================================================================
# Per-contract GEX
# ===========================================================================

def test_gex_per_contract_call_positive_put_negative():
    """Sign convention check: call → +GEX, put → −GEX."""
    contracts = [
        Contract(strike=100, expiry_utc=REF_NOW + timedelta(days=14),
                 is_call=True, oi=100, iv=0.20, T=14/365),
        Contract(strike=100, expiry_utc=REF_NOW + timedelta(days=14),
                 is_call=False, oi=100, iv=0.20, T=14/365),
    ]
    per = compute_gex_per_contract(S=100.0, contracts=contracts, r=0.04, q=0.01)
    assert per[0] > 0       # call
    assert per[1] < 0       # put
    # Same |OI|, |IV|, |T|, |K|, so magnitudes should be equal.
    np.testing.assert_allclose(per[0], -per[1], rtol=1e-10)


def test_gex_per_contract_formula_matches_manual():
    """Validate GEX = Γ · OI · 100 · S² · 0.01 · sign explicitly."""
    S, K, T, r, q, iv, oi = 100.0, 105.0, 30/365, 0.04, 0.01, 0.20, 250
    c = Contract(strike=K, expiry_utc=REF_NOW + timedelta(days=30),
                 is_call=True, oi=oi, iv=iv, T=T)
    per = compute_gex_per_contract(S=S, contracts=[c], r=r, q=q)
    expected = float(bs_gamma(S, K, T, r, q, iv)) * oi * 100 * S * S * 0.01
    np.testing.assert_allclose(per[0], expected, rtol=1e-12)


def test_gex_per_contract_gamma_clamp_drops_anomalies():
    """A contract with anomalously high gamma should contribute 0 GEX."""
    # Construct a contract whose BS gamma exceeds GAMMA_MAX.
    # 0DTE deep ATM with tiny IV → enormous gamma.
    # T=T_MIN (0.5/365), IV=0.05 (min allowed), K=S=100 → very large gamma.
    bad = Contract(
        strike=100.0, expiry_utc=REF_NOW + timedelta(hours=12),
        is_call=True, oi=10000, iv=0.05, T=0.5 / 365,
    )
    raw_gamma = float(bs_gamma(100.0, 100.0, 0.5/365, 0.04, 0.01, 0.05))
    # Confirm we constructed an anomaly.
    assert raw_gamma > GAMMA_MAX

    per = compute_gex_per_contract(S=100.0, contracts=[bad], r=0.04, q=0.01)
    # Should be filtered → 0.
    assert per[0] == 0.0


def test_gex_per_contract_empty():
    per = compute_gex_per_contract(S=100.0, contracts=[], r=0.04, q=0.01)
    assert per.shape == (0,)


# ===========================================================================
# Per-strike aggregation
# ===========================================================================

def test_gex_by_strike_aggregates_same_strike():
    """Two contracts at the same strike should sum together."""
    contracts = [
        Contract(strike=100, expiry_utc=REF_NOW + timedelta(days=14),
                 is_call=True, oi=100, iv=0.20, T=14/365),
        Contract(strike=100, expiry_utc=REF_NOW + timedelta(days=14),
                 is_call=True, oi=200, iv=0.20, T=14/365),
        Contract(strike=105, expiry_utc=REF_NOW + timedelta(days=14),
                 is_call=True, oi=50, iv=0.22, T=14/365),
    ]
    by_strike = compute_gex_by_strike(S=100.0, contracts=contracts, r=0.04, q=0.01)
    assert sorted(by_strike.keys()) == [100.0, 105.0]
    # Strike 100 should be 3x the GEX of a single OI=100 contract.
    one = compute_gex_per_contract(100.0, [contracts[0]], 0.04, 0.01)[0]
    np.testing.assert_allclose(by_strike[100.0], one * 3.0, rtol=1e-10)


def test_gex_by_strike_call_put_offset():
    """At the same strike, equal call/put OI → near-zero net GEX (signs cancel)."""
    contracts = [
        Contract(strike=100, expiry_utc=REF_NOW + timedelta(days=14),
                 is_call=True, oi=100, iv=0.20, T=14/365),
        Contract(strike=100, expiry_utc=REF_NOW + timedelta(days=14),
                 is_call=False, oi=100, iv=0.20, T=14/365),
    ]
    by_strike = compute_gex_by_strike(S=100.0, contracts=contracts, r=0.04, q=0.01)
    assert abs(by_strike[100.0]) < 1e-6


def test_oi_by_strike_unsigned():
    """OI aggregation is unsigned — both calls and puts add."""
    contracts = [
        Contract(strike=100, expiry_utc=REF_NOW + timedelta(days=14),
                 is_call=True, oi=300, iv=0.20, T=14/365),
        Contract(strike=100, expiry_utc=REF_NOW + timedelta(days=14),
                 is_call=False, oi=200, iv=0.22, T=14/365),
    ]
    oi = compute_oi_by_strike(contracts)
    assert oi == {100.0: 500}


# ===========================================================================
# Net GEX at hypothetical spots — for the flip sweep
# ===========================================================================

def test_net_gex_at_spots_vectorized_matches_scalar():
    """Vectorized sweep result must match scalar evaluation per spot."""
    contracts = [
        Contract(strike=95, expiry_utc=REF_NOW + timedelta(days=14),
                 is_call=False, oi=500, iv=0.22, T=14/365),
        Contract(strike=100, expiry_utc=REF_NOW + timedelta(days=14),
                 is_call=True, oi=300, iv=0.20, T=14/365),
        Contract(strike=105, expiry_utc=REF_NOW + timedelta(days=14),
                 is_call=True, oi=200, iv=0.21, T=14/365),
    ]
    spots = np.array([95.0, 100.0, 105.0, 110.0])
    vec = net_gex_at_spots(spots, contracts, r=0.04, q=0.01)

    for i, s in enumerate(spots):
        scalar_total = float(compute_gex_per_contract(
            float(s), contracts, 0.04, 0.01
        ).sum())
        np.testing.assert_allclose(vec[i], scalar_total, rtol=1e-10)


# ===========================================================================
# Gamma flip — the key sanity-checked invariant
# ===========================================================================

def test_find_gamma_flip_synthetic_balanced_chain():
    """
    Construct a chain where the flip is exactly at S by symmetry:
    equal-sized call and put OI at the same strike will produce zero net GEX
    AT THAT STRIKE, but the flip across the broader chain is determined by
    where call wings vs put wings balance out.

    A cleaner construction: heavy puts BELOW spot, heavy calls ABOVE spot.
    Below spot, puts dominate → negative GEX (short gamma regime).
    Above spot, calls dominate → positive GEX (long gamma regime).
    Flip should land near spot.
    """
    exp_date = REF_NOW + timedelta(days=30)
    contracts = [
        # Heavy puts at K=95 (5% below)
        Contract(strike=95, expiry_utc=exp_date, is_call=False,
                 oi=10000, iv=0.20, T=30/365),
        # Heavy calls at K=105 (5% above)
        Contract(strike=105, expiry_utc=exp_date, is_call=True,
                 oi=10000, iv=0.20, T=30/365),
    ]
    flip = find_gamma_flip(S=100.0, contracts=contracts, r=0.04, q=0.01)
    assert flip is not None
    # By symmetry the flip is very near 100. Allow 1% tolerance.
    assert 99.0 < flip < 101.0


def test_find_gamma_flip_returns_none_when_all_positive():
    """All calls → all GEX positive → no zero-crossing → None."""
    exp_date = REF_NOW + timedelta(days=30)
    contracts = [
        Contract(strike=100, expiry_utc=exp_date, is_call=True,
                 oi=500, iv=0.20, T=30/365),
        Contract(strike=105, expiry_utc=exp_date, is_call=True,
                 oi=300, iv=0.20, T=30/365),
    ]
    flip = find_gamma_flip(S=100.0, contracts=contracts, r=0.04, q=0.01)
    assert flip is None


def test_find_gamma_flip_returns_none_for_empty():
    assert find_gamma_flip(S=100.0, contracts=[], r=0.04, q=0.01) is None


def test_find_gamma_flip_picks_nearest_crossing():
    """
    Construct two zero-crossings; the function should return the one nearest
    current spot. We do this with three OI clusters arranged so net GEX
    looks like: +, then dips negative, then rises positive again.
    """
    exp_date = REF_NOW + timedelta(days=30)
    # Calls at 92, puts at 100, calls at 108 — creates crossings on both sides.
    contracts = [
        Contract(strike=92, expiry_utc=exp_date, is_call=True,
                 oi=8000, iv=0.20, T=30/365),
        Contract(strike=100, expiry_utc=exp_date, is_call=False,
                 oi=8000, iv=0.20, T=30/365),
        Contract(strike=108, expiry_utc=exp_date, is_call=True,
                 oi=8000, iv=0.20, T=30/365),
    ]
    # With spot at 102, the nearer crossing should be on the upper side
    # (between 100 and 108). With spot at 98, the nearer crossing should be
    # on the lower side (between 92 and 100).
    flip_high = find_gamma_flip(S=102.0, contracts=contracts, r=0.04, q=0.01)
    flip_low = find_gamma_flip(S=98.0, contracts=contracts, r=0.04, q=0.01)
    assert flip_high is not None
    assert flip_low is not None
    assert flip_high > 100.0
    assert flip_low < 100.0


# ===========================================================================
# Walls & clusters
# ===========================================================================

def test_walls_clusters_basic():
    """Verify call/put wall identification on a simple synthetic GEX map."""
    gex_by_strike = {
        95.0:  -2_000_000_000.0,   # PW (most negative)
        97.5:  -800_000_000.0,
        100.0: -500_000_000.0,
        102.5:  600_000_000.0,
        105.0: 1_800_000_000.0,    # CW (most positive)
        107.5:  900_000_000.0,
    }
    oi_by_strike = {
        95.0:  20000,
        100.0: 30000,             # max OI
        105.0: 18000,
        107.5: 12000,
        110.0: 5000,
    }
    result = identify_walls_clusters(gex_by_strike, oi_by_strike,
                                     top_n_clusters=3, top_n_oi=3)

    assert result["call_wall"]["strike"] == 105.0
    assert result["put_wall"]["strike"] == 95.0

    # Clusters exclude the walls themselves.
    pos_strikes = [c["strike"] for c in result["top_pos_clusters"]]
    neg_strikes = [c["strike"] for c in result["top_neg_clusters"]]
    assert 105.0 not in pos_strikes
    assert 95.0 not in neg_strikes

    # Pos clusters sorted high to low.
    pos_vals = [c["gex_dollars"] for c in result["top_pos_clusters"]]
    assert pos_vals == sorted(pos_vals, reverse=True)
    # Neg clusters sorted most-negative first.
    neg_vals = [c["gex_dollars"] for c in result["top_neg_clusters"]]
    assert neg_vals == sorted(neg_vals)

    # OI clusters sorted high to low.
    oi_vals = [c["open_interest"] for c in result["top_oi_clusters"]]
    assert oi_vals == sorted(oi_vals, reverse=True)
    assert len(result["top_oi_clusters"]) == 3

    # Total net GEX = sum of map.
    np.testing.assert_allclose(result["total_net_gex"],
                                sum(gex_by_strike.values()), rtol=1e-10)


def test_walls_clusters_empty_chain():
    result = identify_walls_clusters({}, {}, top_n_clusters=3, top_n_oi=3)
    assert result["call_wall"] is None
    assert result["put_wall"] is None
    assert result["top_pos_clusters"] == []
    assert result["top_neg_clusters"] == []
    assert result["top_oi_clusters"] == []
    assert result["total_net_gex"] == 0.0


def test_walls_clusters_all_positive():
    """Edge case: no negative GEX anywhere."""
    gex = {100.0: 1e9, 105.0: 2e9, 110.0: 5e8}
    result = identify_walls_clusters(gex, {}, top_n_clusters=5, top_n_oi=5)
    assert result["call_wall"]["strike"] == 105.0
    assert result["put_wall"] is None
    assert result["top_neg_clusters"] == []


# ===========================================================================
# End-to-end on synthetic chain — sanity check the whole pipeline
# ===========================================================================

def test_end_to_end_synthetic_pipeline():
    """
    Build a small-but-realistic synthetic chain and run the full pipeline.
    Verify the call wall, put wall, and flip make sense relative to where
    the OI clusters are positioned.
    """
    spot = 100.0
    exp = REF_NOW + timedelta(days=14)

    options = []
    # Range of strikes around spot.
    strikes = list(range(85, 116))  # 85, 86, ..., 115
    rng = np.random.default_rng(42)

    for K in strikes:
        # Modest baseline OI everywhere.
        base_oi = int(50 + rng.integers(0, 100))
        # Heavy call wall at K=110.
        call_oi = base_oi + (5000 if K == 110 else 0)
        # Heavy put wall at K=90.
        put_oi = base_oi + (5000 if K == 90 else 0)

        # IV smile — higher for OTM puts.
        iv_call = 0.18 + 0.001 * max(0, K - spot)
        iv_put = 0.18 + 0.002 * max(0, spot - K)

        options.append({
            "option": _occ("SPY", exp, True, float(K)),
            "open_interest": call_oi, "iv": iv_call,
        })
        options.append({
            "option": _occ("SPY", exp, False, float(K)),
            "open_interest": put_oi, "iv": iv_put,
        })

    chain = _make_chain(options, spot=spot)
    parsed_spot, contracts = parse_chain(chain, now_utc=REF_NOW)
    assert parsed_spot == spot
    assert len(contracts) == 2 * len(strikes)

    gex_map = compute_gex_by_strike(parsed_spot, contracts, r=0.04, q=0.013)
    oi_map = compute_oi_by_strike(contracts)
    walls = identify_walls_clusters(gex_map, oi_map)
    flip = find_gamma_flip(parsed_spot, contracts, r=0.04, q=0.013)

    # Call wall should be at K=110 (where we placed heavy call OI).
    assert walls["call_wall"]["strike"] == 110.0
    # Put wall should be at K=90 (where we placed heavy put OI).
    assert walls["put_wall"]["strike"] == 90.0
    # Flip should sit somewhere between the heavy put wall (90) and call wall (110).
    # By construction it should be near 100 (symmetric clusters), give or take a
    # few percent for the IV-smile asymmetry.
    assert flip is not None
    assert 95.0 < flip < 105.0
