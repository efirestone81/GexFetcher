"""Unit tests for futures_mapper.py."""

import math

import pytest

from ffgex_fetcher.futures_mapper import (
    TickerConfig,
    DEFAULT_TICKERS,
    compute_multiplier,
    compute_carry_basis,
    map_strike,
)


def test_default_tickers_present():
    for sym in ("SPY", "QQQ", "IWM", "DIA", "GLD", "USO"):
        assert sym in DEFAULT_TICKERS


# ---------------------------------------------------------------------------
# Dynamic multiplier (Method A)
# ---------------------------------------------------------------------------

def test_compute_multiplier_in_bounds():
    cfg = DEFAULT_TICKERS["SPY"]
    # ETF spot near ref_price/10 -> multiplier near 10.
    mult, warnings = compute_multiplier(etf_spot=580.0, cfg=cfg)
    assert mult == pytest.approx(5800.0 / 580.0)
    assert warnings == []


def test_compute_multiplier_out_of_bounds_warns():
    cfg = DEFAULT_TICKERS["SPY"]
    # ETF spot at 700 -> multiplier ~ 8.28, outside [9.5, 10.5].
    mult, warnings = compute_multiplier(etf_spot=700.0, cfg=cfg)
    assert mult == pytest.approx(5800.0 / 700.0)
    assert len(warnings) == 1
    assert "outside bounds" in warnings[0]


def test_compute_multiplier_invalid_spot():
    cfg = DEFAULT_TICKERS["SPY"]
    mult, warnings = compute_multiplier(etf_spot=0.0, cfg=cfg)
    assert mult == 1.0
    assert any("invalid" in w for w in warnings)


# ---------------------------------------------------------------------------
# Cost-of-carry basis (Method B)
# ---------------------------------------------------------------------------

def test_carry_basis_positive_when_r_gt_q():
    """When risk-free rate exceeds dividend yield, futures trade above cash."""
    cfg = TickerConfig("SPY", "ES", 5800.0, 10.0, (9.5, 10.5), 0.0125)
    basis = compute_carry_basis(etf_spot=580.0, r=0.043, cfg=cfg, T_years=0.25)
    assert basis > 0
    # Roughly: 580 * (e^((0.043-0.0125)*0.25) - 1) * 10
    expected = 580.0 * (math.exp((0.043 - 0.0125) * 0.25) - 1) * 10
    assert basis == pytest.approx(expected, rel=1e-10)


def test_carry_basis_negative_when_q_gt_r():
    """If dividend yield exceeds rate (rare), futures trade below cash."""
    cfg = TickerConfig("SPY", "ES", 5800.0, 10.0, (9.5, 10.5), dividend_yield=0.06)
    basis = compute_carry_basis(etf_spot=580.0, r=0.04, cfg=cfg, T_years=0.25)
    assert basis < 0


def test_carry_basis_zero_for_zero_T():
    cfg = DEFAULT_TICKERS["SPY"]
    assert compute_carry_basis(580.0, 0.043, cfg, 0.0) == 0.0


def test_carry_basis_grows_with_T():
    cfg = DEFAULT_TICKERS["SPY"]
    short = compute_carry_basis(580.0, 0.043, cfg, 0.083)  # 1 month
    long = compute_carry_basis(580.0, 0.043, cfg, 0.25)    # 3 months
    assert long > short


# ---------------------------------------------------------------------------
# map_strike
# ---------------------------------------------------------------------------

def test_map_strike_dynamic_multiplier_basic():
    """A SPY $580 strike under multiplier=10.0 should map to ES 5800."""
    cfg = DEFAULT_TICKERS["SPY"]
    mapped = map_strike(etf_strike=580.0, multiplier=10.0, basis_carry=47.2, cfg=cfg)
    assert mapped["etf_strike"] == 580.0
    assert mapped["futures_mult"] == 5800.0
    # Carry method: 580 * 10 + 47.2 = 5847.2
    assert mapped["futures_basis"] == pytest.approx(5847.2)


def test_map_strike_methods_close_in_typical_case():
    """In normal conditions, multiplier and carry methods should be close
    (within ~1% of each other) - they're solving the same problem."""
    cfg = DEFAULT_TICKERS["SPY"]
    spot = 580.0
    r = 0.043
    T = 0.083  # 1 month
    mult, _ = compute_multiplier(spot, cfg)
    basis = compute_carry_basis(spot, r, cfg, T)
    mapped = map_strike(580.0, mult, basis, cfg)
    diff = abs(mapped["futures_mult"] - mapped["futures_basis"])
    # Both methods near 5800+-50, so absolute difference under $100.
    assert diff < 100.0


def test_map_strike_negative_basis():
    cfg = TickerConfig("FOO", "FU", 1000.0, 10.0, (9.0, 11.0), dividend_yield=0.10)
    mapped = map_strike(100.0, multiplier=10.0, basis_carry=-15.0, cfg=cfg)
    assert mapped["futures_basis"] == pytest.approx(100 * 10 - 15.0)