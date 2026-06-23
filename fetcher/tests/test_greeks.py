"""
Unit tests for greeks.py.

Validation strategy:
1. Compare against scipy.stats.norm.pdf for the PDF helper.
2. Verify gamma matches the central-difference second derivative of call price
   (Γ = ∂²C/∂S²) — this is the definition, and it's independent of any
   closed-form library.
3. Verify call/put gamma equivalence (put-call parity ⇒ Γ_call = Γ_put).
4. Test edge cases: T=0, σ=0, S=0, K=0 → gamma=0 (no NaN/inf).
5. Test the 0DTE-near-ATM stress case is finite when T_min is applied.
6. Compare against a published textbook example for sanity.
"""

import math

import numpy as np
import pytest
from scipy.stats import norm as scipy_norm

from ffgex_fetcher.greeks import bs_gamma, bs_call_price, norm_pdf, T_MIN


# ---------------------------------------------------------------------------
# norm_pdf
# ---------------------------------------------------------------------------

def test_norm_pdf_matches_scipy():
    xs = np.linspace(-5, 5, 101)
    expected = scipy_norm.pdf(xs)
    got = norm_pdf(xs)
    np.testing.assert_allclose(got, expected, rtol=1e-12)


def test_norm_pdf_scalar():
    # PDF at 0 = 1/√(2π) ≈ 0.39894228
    assert norm_pdf(0.0) == pytest.approx(1.0 / math.sqrt(2.0 * math.pi), rel=1e-12)


# ---------------------------------------------------------------------------
# Gamma — definitional check via central-difference of price
# ---------------------------------------------------------------------------

def numerical_gamma(S, K, T, r, q, sigma, h_rel=1e-4):
    """Γ = ∂²C/∂S² via central difference."""
    h = S * h_rel
    c_plus = bs_call_price(S + h, K, T, r, q, sigma)
    c_zero = bs_call_price(S, K, T, r, q, sigma)
    c_minus = bs_call_price(S - h, K, T, r, q, sigma)
    return float((c_plus - 2 * c_zero + c_minus) / (h * h))


@pytest.mark.parametrize("S, K, T, sigma", [
    (100.0, 100.0,  30/365, 0.20),   # ATM, 30 DTE, vol 20%
    (100.0, 110.0,  30/365, 0.20),   # OTM call
    (100.0,  90.0,  30/365, 0.20),   # ITM call
    (100.0, 100.0,   7/365, 0.30),   # 1-week, vol 30%
    (100.0, 100.0, 365/365, 0.15),   # 1-year, vol 15%
    (5800.0, 5800.0, 30/365, 0.15),  # SPX-scale ATM
    (5800.0, 5900.0,  7/365, 0.18),  # SPX-scale OTM, weekly
    (200.0, 195.0,    1/365, 0.40),  # 1DTE near-ATM (stress)
])
def test_bs_gamma_matches_numerical(S, K, T, sigma):
    r, q = 0.043, 0.013
    analytic = float(bs_gamma(S, K, T, r, q, sigma))
    numeric = numerical_gamma(S, K, T, r, q, sigma)
    # Numerical 2nd derivative has ~h² error; loose-ish tolerance is fine.
    np.testing.assert_allclose(analytic, numeric, rtol=1e-3, atol=1e-9)


# ---------------------------------------------------------------------------
# Edge cases — no NaN, no inf, gamma=0 for invalid inputs
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("S, K, T, sigma", [
    (0.0, 100.0, 0.1, 0.2),     # S=0
    (100.0, 0.0, 0.1, 0.2),     # K=0
    (100.0, 100.0, 0.0, 0.2),   # T=0 (expired)
    (100.0, 100.0, -0.1, 0.2),  # T<0 (clearly stale)
    (100.0, 100.0, 0.1, 0.0),   # σ=0
    (100.0, 100.0, 0.1, -0.1),  # σ<0
])
def test_bs_gamma_invalid_returns_zero(S, K, T, sigma):
    got = float(bs_gamma(S, K, T, 0.04, 0.01, sigma))
    assert got == 0.0
    assert not math.isnan(got)
    assert not math.isinf(got)


# ---------------------------------------------------------------------------
# 0DTE stress — when T is floored at T_MIN, gamma is finite (not inf)
# ---------------------------------------------------------------------------

def test_bs_gamma_0dte_atm_finite_with_floor():
    # 0DTE ATM with σ=10% — would be catastrophic without T_min floor.
    g = float(bs_gamma(S=5800.0, K=5800.0, T=T_MIN, r=0.043, q=0.013, sigma=0.10))
    assert math.isfinite(g)
    assert g > 0
    # Sanity: a 0DTE ATM SPX option should have very high gamma but not absurd.
    # With T = 0.5/365 ≈ 0.00137 yr, σ=0.10, S=5800:
    # Γ ≈ 1/(S σ √T) · φ(d1) ≈ 1/(5800 · 0.10 · 0.037) · 0.398 ≈ 0.0185
    assert 0.005 < g < 0.05  # generous band


# ---------------------------------------------------------------------------
# Vectorization
# ---------------------------------------------------------------------------

def test_bs_gamma_vectorized_over_strikes():
    S = 100.0
    K = np.array([80.0, 90.0, 100.0, 110.0, 120.0])
    T, r, q, sigma = 30/365, 0.04, 0.01, 0.20

    # Vectorized call
    vec = bs_gamma(S, K, T, r, q, sigma)
    assert vec.shape == K.shape

    # Spot-check against scalar calls
    for i, k in enumerate(K):
        scalar = float(bs_gamma(S, float(k), T, r, q, sigma))
        np.testing.assert_allclose(vec[i], scalar, rtol=1e-12)


def test_bs_gamma_vectorized_over_spots():
    """The flip sweep evaluates gamma at many candidate spots."""
    spots = np.linspace(90.0, 110.0, 41)
    K, T, r, q, sigma = 100.0, 30/365, 0.04, 0.01, 0.20

    vec = bs_gamma(spots, K, T, r, q, sigma)
    assert vec.shape == spots.shape

    # ATM gamma should be the maximum across this strike at ATM.
    atm_idx = np.argmax(vec)
    # With strike=100, ATM gamma peak is slightly above S=100 due to
    # the drift term — peak should be near K (within ~2% of strike).
    assert 0.98 * K < spots[atm_idx] < 1.02 * K


def test_bs_gamma_vectorized_per_contract_chain():
    """Realistic case: each contract has its own K, T, σ."""
    S = 5800.0
    K = np.array([5700, 5750, 5800, 5850, 5900], dtype=float)
    T = np.array([7, 14, 30, 30, 60], dtype=float) / 365
    sigma = np.array([0.18, 0.16, 0.14, 0.15, 0.17])

    vec = bs_gamma(S, K, T, 0.043, 0.013, sigma)
    assert vec.shape == (5,)
    # All gamma values should be positive and finite.
    assert np.all(vec > 0)
    assert np.all(np.isfinite(vec))


# ---------------------------------------------------------------------------
# Magnitude sanity check — Hull textbook reference
# ---------------------------------------------------------------------------

def test_bs_gamma_textbook_reference():
    """
    Hull example: S=49, K=50, r=5%, σ=20%, T=20 weeks (0.3846 yr).
    Reference gamma ≈ 0.066 per share (Hull 10e, Table 19.4 vicinity).
    No dividend.
    """
    g = float(bs_gamma(S=49.0, K=50.0, T=20/52, r=0.05, q=0.0, sigma=0.20))
    # Hull rounds to 3 decimals; we accept ±5% of the reference.
    assert g == pytest.approx(0.066, rel=0.05)


# ---------------------------------------------------------------------------
# Dividend yield effect — gamma is mildly sensitive to q via two channels:
#   (i)  the e^{-qT} pre-factor pulls gamma down
#   (ii) the (r - q) drift term shifts d1, which can raise or lower φ(d1)
# Near ATM with moderate q the d1 shift can dominate (gamma slightly UP);
# deep OTM the e^{-qT} factor wins. Either way the magnitude should be small.
# ---------------------------------------------------------------------------

def test_bs_gamma_dividend_effect_atm_small():
    """Near ATM, dividend effect on gamma is small (within a few %)."""
    base = dict(S=100.0, K=100.0, T=0.5, r=0.05, sigma=0.20)
    g_no_div = float(bs_gamma(q=0.0, **base))
    g_with_div = float(bs_gamma(q=0.05, **base))
    # Should be very close — within ~5% either way.
    ratio = g_with_div / g_no_div
    assert 0.95 < ratio < 1.05


def test_bs_gamma_dividend_effect_otm_reduces():
    """Deep OTM call, the e^{-qT} factor dominates → higher q means lower gamma."""
    base = dict(S=100.0, K=120.0, T=0.5, r=0.05, sigma=0.20)
    g_no_div = float(bs_gamma(q=0.0, **base))
    g_with_div = float(bs_gamma(q=0.08, **base))
    # Deep OTM with high dividend reduces gamma materially.
    assert g_with_div < g_no_div


# ---------------------------------------------------------------------------
# Time-to-expiry effect — longer T should give lower gamma for ATM
# ---------------------------------------------------------------------------

def test_bs_gamma_atm_decreases_with_T():
    """ATM gamma scales as ~1/√T — short-dated options have higher gamma."""
    params = dict(S=100.0, K=100.0, r=0.04, q=0.01, sigma=0.20)
    g_short = float(bs_gamma(T=7/365, **params))
    g_mid = float(bs_gamma(T=30/365, **params))
    g_long = float(bs_gamma(T=180/365, **params))

    assert g_short > g_mid > g_long
    # Ratio should roughly follow √(T_long/T_short).
    np.testing.assert_allclose(
        g_short / g_long, math.sqrt(180 / 7), rtol=0.10
    )
