"""
Futures mapping. Converts an underlying strike to a futures price.

For INDEX underlyings (SPX, NDX), the index and the future are the same scale
(SPX ~7400, ES ~7425), so scale_to_index = 1.0 and the only adjustment is the
cost-of-carry basis (ES = SPX + basis). The dynamic multiplier resolves to
~1.003 naturally.

For ETF underlyings (IWM, DIA, GLD, USO), the ETF is a fraction of the index,
so scale_to_index is ~10/40/etc and the multiplier absorbs the ratio.

Method A — Dynamic Multiplier (default, self-correcting):
    multiplier = futures_ref_price / underlying_spot
    futures_price = strike * multiplier

Method B — Cost-of-Carry Basis:
    basis = underlying_spot * (exp((r - q) * T) - 1) * scale
    futures_price = strike * scale + basis
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TickerConfig:
    # NOTE: the first six fields preserve the original positional order so that
    # existing call sites/tests using positional args keep working:
    #   TickerConfig(etf_symbol, futures_symbol, futures_ref_price,
    #                scale_to_index, multiplier_bounds, dividend_yield)
    etf_symbol: str           # underlying ticker, e.g. "SPY", "_SPX"
    futures_symbol: str       # e.g. "ES"
    futures_ref_price: float  # last known front-month futures price
    scale_to_index: float     # 1.0 for index, ~10/40 for ETFs
    multiplier_bounds: tuple[float, float]
    dividend_yield: float
    # New optional fields (added for SPX/NDX). Default to etf_symbol so older
    # 6-arg construction still works.
    cboe_symbol: str = ""     # CBOE CDN symbol; defaults to etf_symbol if blank
    display_name: str = ""    # human label; defaults to etf_symbol if blank

    @property
    def symbol(self) -> str:
        """CBOE CDN fetch symbol (e.g. '_SPX'). Falls back to etf_symbol."""
        return self.cboe_symbol or self.etf_symbol

    @property
    def label(self) -> str:
        return self.display_name or self.etf_symbol


# ES and NQ source from the cash INDEX (SPX, NDX) — the same underlying the
# futures track — for maximum accuracy. The other four stay on ETF proxies.
# Index underlyings use the CBOE underscore-prefixed symbol (_SPX, _NDX),
# passed as the cboe_symbol, with scale_to_index = 1.0.
DEFAULT_TICKERS: dict[str, TickerConfig] = {
    # --- INDEX-sourced (primary instruments) ---
    # etf_symbol here doubles as the key's identity; cboe_symbol is the fetch URL symbol.
    "SPX": TickerConfig("SPX", "ES", 7425.0, 1.0, (0.97, 1.03), 0.0125, cboe_symbol="_SPX", display_name="SPX"),
    "NDX": TickerConfig("NDX", "NQ", 29441.0, 1.0, (0.97, 1.03), 0.0070, cboe_symbol="_NDX", display_name="NDX"),

    # --- ETF-sourced (secondary instruments) ---
    "IWM": TickerConfig("IWM", "RTY", 2980.0, 10.0,  (9.5, 10.7),  0.0135),
    "DIA": TickerConfig("DIA", "YM", 47000.0, 90.0,  (87.0, 92.0), 0.0160),
    "GLD": TickerConfig("GLD", "GC",  4006.0, 11.0,  (10.5, 11.5), 0.0),
    "USO": TickerConfig("USO", "CL",    77.0, 0.72,  (0.65, 0.80), 0.0),

    # --- ETF proxies, available but NOT fetched by default ---
    # ES/NQ now source from SPX/NDX (above). SPY/QQQ remain configured so they
    # can be requested explicitly (TICKERS=SPY) and so the SPY-based tests
    # (live integration + synthetic fixtures) resolve a config. Because they
    # are NOT in DEFAULT_FETCH_TICKERS, their futures_ref_price is calibration
    # for the test fixtures only and has no production effect. Kept at the
    # original values the synthetic fixtures were built against.
    "SPY": TickerConfig("SPY", "ES",  5800.0, 10.0, (9.5, 10.5),  0.0125, display_name="SPY"),
    "QQQ": TickerConfig("QQQ", "NQ", 20000.0, 40.0, (39.0, 43.0), 0.0070, display_name="QQQ"),
}

# The tickers fetched when no explicit TICKERS env var is given. SPY/QQQ are in
# DEFAULT_TICKERS for config/lookup but intentionally excluded from the default
# fetch set, since ES/NQ are served by SPX/NDX.
DEFAULT_FETCH_TICKERS = ["SPX", "NDX", "IWM", "DIA", "GLD", "USO"]


def compute_multiplier(etf_spot, cfg):
    if etf_spot <= 0:
        return 1.0, [f"{cfg.label}: invalid spot ({etf_spot})"]
    multiplier = cfg.futures_ref_price / etf_spot
    warnings = []
    lo, hi = cfg.multiplier_bounds
    if not (lo <= multiplier <= hi):
        warnings.append(
            f"{cfg.label}: multiplier {multiplier:.4f} outside bounds "
            f"[{lo}, {hi}] — update futures_ref_price (currently {cfg.futures_ref_price})"
        )
    return multiplier, warnings


def compute_carry_basis(etf_spot, r, cfg, T_years):
    if etf_spot <= 0 or T_years <= 0:
        return 0.0
    net_carry = r - cfg.dividend_yield
    return etf_spot * (math.exp(net_carry * T_years) - 1.0) * cfg.scale_to_index


def map_strike(etf_strike, multiplier, basis_carry, cfg):
    return {
        "etf_strike": float(etf_strike),    # underlying strike (index or ETF)
        "futures_mult": float(etf_strike * multiplier),
        "futures_basis": float(etf_strike * cfg.scale_to_index + basis_carry),
    }