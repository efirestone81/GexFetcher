"""
Futures mapping.

Two methods are provided for converting an ETF strike to a futures price:

Method A — Dynamic Multiplier (default, self-correcting):
    multiplier = futures_ref_price / etf_spot
    futures_price = etf_strike * multiplier

    This is fast, has no dividend/rate assumptions, and self-corrects as both
    prices move together. The trade-off: multiplier is only as good as the
    `futures_ref_price` constant; production should refresh that constant
    against live front-month settlement periodically.

Method B — Cost-of-Carry Basis (rigorous):
    basis = etf_spot * (exp((r - q) * T_front_month) - 1) * scale
    futures_price = etf_strike * scale + basis

    Uses Black-Scholes-equivalent forward pricing. More accurate when r and q
    are current, but adds dependency on those macro inputs.

Both methods are computed for every level so the indicator can switch at
display time without re-fetching.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TickerConfig:
    """Configuration for one ETF underlying mapped to a futures contract."""
    etf_symbol: str          # e.g. "SPY"
    futures_symbol: str      # e.g. "ES"
    futures_ref_price: float # last known futures front-month price (for Method A)
    scale_to_index: float    # static scale: SPY×10≈SPX, QQQ×40≈NDX, IWM×10≈RUT
    multiplier_bounds: tuple[float, float]   # alarm range for Method A
    dividend_yield: float    # annualized continuous yield for Method B


# Default config for the v1 ticker set. The futures_ref_price values are
# starting points; the fetcher should periodically refresh them.
DEFAULT_TICKERS: dict[str, TickerConfig] = {
    "SPY": TickerConfig("SPY", "ES",  5800.0, 10.0,  (9.5, 10.5),  0.0125),
    "QQQ": TickerConfig("QQQ", "NQ", 21000.0, 40.0,  (38.0, 42.0), 0.0070),
    "IWM": TickerConfig("IWM", "RTY", 2300.0, 10.0,  (9.5, 10.5),  0.0135),
    "DIA": TickerConfig("DIA", "YM", 43000.0, 100.0, (99.0, 101.0),0.0160),
    "GLD": TickerConfig("GLD", "GC",  3000.0, 12.0,  (11.0, 13.0), 0.0),
    "USO": TickerConfig("USO", "CL",    80.0, 1.0,   (0.5, 1.5),   0.0),
}


def compute_multiplier(
    etf_spot: float,
    cfg: TickerConfig,
) -> tuple[float, list[str]]:
    """
    Compute the dynamic multiplier and any warnings.

    Returns (multiplier, warnings). Multiplier is etf_spot's mapping to the
    futures front-month reference price. If the result falls outside the
    configured bounds, a warning is emitted (the bounds are a sanity check
    against a stale futures_ref_price).
    """
    if etf_spot <= 0:
        return 1.0, [f"{cfg.etf_symbol}: invalid etf_spot ({etf_spot})"]

    multiplier = cfg.futures_ref_price / etf_spot
    warnings: list[str] = []
    lo, hi = cfg.multiplier_bounds
    if not (lo <= multiplier <= hi):
        warnings.append(
            f"{cfg.etf_symbol}: multiplier {multiplier:.4f} outside bounds "
            f"[{lo}, {hi}] — update futures_ref_price (currently "
            f"{cfg.futures_ref_price})"
        )
    return multiplier, warnings


def compute_carry_basis(
    etf_spot: float,
    r: float,
    cfg: TickerConfig,
    T_years: float,
) -> float:
    """
    Cost-of-carry basis in futures-price units.

        basis_futures = etf_spot * (e^((r - q) * T) - 1) * scale

    Where (r - q) is the net carry rate and T is years to the futures
    contract's expiration. Use the active front-month expiration for T
    (about 0.05–0.25 years typically for an ES/NQ front month).
    """
    if etf_spot <= 0 or T_years <= 0:
        return 0.0
    net_carry = r - cfg.dividend_yield
    return etf_spot * (math.exp(net_carry * T_years) - 1.0) * cfg.scale_to_index


def map_strike(
    etf_strike: float,
    multiplier: float,
    basis_carry: float,
    cfg: TickerConfig,
) -> dict[str, float]:
    """
    Map an ETF strike to its futures-equivalent prices under both methods.

    Returns a dict with three keys (the indicator picks one):
      - etf_strike: original ETF strike (unmodified)
      - futures_mult: under Method A (dynamic multiplier)
      - futures_basis: under Method B (cost-of-carry)
    """
    return {
        "etf_strike": float(etf_strike),
        "futures_mult": float(etf_strike * multiplier),
        "futures_basis": float(etf_strike * cfg.scale_to_index + basis_carry),
    }
