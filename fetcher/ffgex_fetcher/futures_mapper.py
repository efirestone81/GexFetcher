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
    symbol: str               # CBOE CDN symbol, e.g. "_SPX", "_NDX", "SPY"
    futures_symbol: str       # e.g. "ES"
    futures_ref_price: float  # last known front-month futures price
    scale_to_index: float     # 1.0 for index, ~10/40 for ETFs
    multiplier_bounds: tuple[float, float]
    dividend_yield: float
    display_name: str         # human label, e.g. "SPX"


# v1 config. ES and NQ now source from the cash INDEX (SPX, NDX) — the same
# underlying the futures track — for maximum accuracy. The other four stay on
# ETF proxies since there is no equally clean cash-index option chain for them.
#
# Index underlyings use the CBOE underscore-prefixed symbol (_SPX, _NDX) and
# scale_to_index = 1.0.
DEFAULT_TICKERS: dict[str, TickerConfig] = {
    # --- INDEX-sourced (primary instruments) ---
    "SPX": TickerConfig("_SPX", "ES", 7425.0, 1.0, (0.97, 1.03), 0.0125, "SPX"),
    "NDX": TickerConfig("_NDX", "NQ", 29441.0, 1.0, (0.97, 1.03), 0.0070, "NDX"),

    # --- ETF-sourced (secondary instruments) ---
    "IWM": TickerConfig("IWM", "RTY", 2980.0, 10.0,  (9.5, 10.7),  0.0135, "IWM"),
    "DIA": TickerConfig("DIA", "YM", 47000.0, 90.0,  (87.0, 92.0), 0.0160, "DIA"),
    "GLD": TickerConfig("GLD", "GC",  4006.0, 11.0,  (10.5, 11.5), 0.0,    "GLD"),
    "USO": TickerConfig("USO", "CL",    77.0, 0.72,  (0.65, 0.80), 0.0,    "USO"),
}


def compute_multiplier(underlying_spot, cfg):
    if underlying_spot <= 0:
        return 1.0, [f"{cfg.display_name}: invalid spot ({underlying_spot})"]
    multiplier = cfg.futures_ref_price / underlying_spot
    warnings = []
    lo, hi = cfg.multiplier_bounds
    if not (lo <= multiplier <= hi):
        warnings.append(
            f"{cfg.display_name}: multiplier {multiplier:.4f} outside bounds "
            f"[{lo}, {hi}] — update futures_ref_price (currently {cfg.futures_ref_price})"
        )
    return multiplier, warnings


def compute_carry_basis(underlying_spot, r, cfg, T_years):
    if underlying_spot <= 0 or T_years <= 0:
        return 0.0
    net_carry = r - cfg.dividend_yield
    return underlying_spot * (math.exp(net_carry * T_years) - 1.0) * cfg.scale_to_index


def map_strike(strike, multiplier, basis_carry, cfg):
    return {
        "etf_strike": float(strike),    # kept as key name for schema compat (= underlying strike)
        "futures_mult": float(strike * multiplier),
        "futures_basis": float(strike * cfg.scale_to_index + basis_carry),
    }
