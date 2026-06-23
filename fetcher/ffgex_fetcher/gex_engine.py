"""
GEX engine.

Pipeline:
  1. parse_chain(json) → spot + list[Contract]
  2. compute_gex_by_strike(spot, contracts) → dict[strike → GEX$]
  3. compute_oi_by_strike(contracts) → dict[strike → OI]
  4. find_gamma_flip(spot, contracts) → float | None
  5. identify_walls_clusters(gex_by_strike, oi_by_strike, top_n) → result dict

The GEX formula uses the SqueezeMetrics dollar-per-1%-move convention:

  GEX_contract = Γ · OI · 100 · S² · 0.01 · sign

where sign = +1 for calls, −1 for puts (dealer-positioning convention).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

import numpy as np

from ffgex_fetcher.greeks import bs_gamma, IV_MIN, IV_MAX, T_MIN


# Contract multiplier for equity-style options (SPY/QQQ/etc).
CONTRACT_MULTIPLIER = 100

# Gamma sanity bound — drop contracts whose computed gamma exceeds this.
# Catches 0DTE feed anomalies (e.g. IV=0.001 giving Γ=200 per share).
GAMMA_MAX = 2.0

# OCC symbol pattern: ROOT + YYMMDD + C/P + STRIKE(8 digits, 3 decimals impl.)
# Examples: SPY240419C00450000, QQQ240517P00380500
_OCC_RE = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


@dataclass(frozen=True, slots=True)
class Contract:
    """Parsed option contract — one row of a chain."""
    strike: float
    expiry_utc: datetime
    is_call: bool
    oi: int
    iv: float
    T: float  # years until expiry, post-floor


def parse_occ_symbol(symbol: str, now_utc: datetime | None = None) -> tuple[float, datetime, bool] | None:
    """
    Parse an OCC-formatted option symbol.

    Returns (strike, expiry_utc, is_call) or None on parse failure.

    OCC encodes the strike as 8 digits with implicit 3 decimal places
    (50450000 → $50,450.000). Expiry is encoded YYMMDD; we assume 4 PM ET
    (≈ 20:00 UTC) as the conventional settlement time — this is approximate
    for AM-settled SPX but is the standard treatment in retail GEX tools.
    """
    m = _OCC_RE.match(symbol)
    if not m:
        return None
    _root, yymmdd, cp, strike_raw = m.groups()
    try:
        yy = int(yymmdd[:2])
        mm = int(yymmdd[2:4])
        dd = int(yymmdd[4:6])
        # Assume 20:00 UTC (≈ 16:00 ET, US options close).
        expiry = datetime(2000 + yy, mm, dd, 20, 0, tzinfo=timezone.utc)
    except ValueError:
        return None
    strike = int(strike_raw) / 1000.0
    return strike, expiry, (cp == "C")


def parse_chain(
    chain_json: dict,
    now_utc: datetime | None = None,
    max_dte: int = 30,
    min_dte: int = -1,
) -> tuple[float, list[Contract]]:
    """
    Parse a CBOE CDN options chain JSON into a list of Contract records.

    Filtering applied here:
    - OI must be > 0
    - IV must be in [IV_MIN, IV_MAX]
    - DTE must be in [min_dte, max_dte]
    - OCC symbol must parse
    Gamma-based filtering (GAMMA_MAX) is applied later, after BS computation.

    Parameters
    ----------
    chain_json : dict
        Parsed JSON from the CBOE CDN endpoint. Must contain
        chain_json["data"]["current_price"] and chain_json["data"]["options"].
    now_utc : datetime, optional
        Reference time for DTE calculation; defaults to datetime.now(timezone.utc).
        Pass explicitly in tests for reproducibility.
    max_dte : int
        Maximum days-to-expiry to include. Default 30 (covers 0DTE through
        next-monthly).
    min_dte : int
        Minimum DTE. Default -1 (allow today's expirations; reject anything
        clearly stale).

    Returns
    -------
    spot : float
    contracts : list[Contract]
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")

    data = chain_json.get("data") or {}
    spot = data.get("current_price")
    if spot is None or spot <= 0:
        raise ValueError("Chain JSON missing or invalid data.current_price")
    spot = float(spot)

    raw_options = data.get("options") or []
    contracts: list[Contract] = []

    for opt in raw_options:
        sym = opt.get("option")
        if not isinstance(sym, str):
            continue
        parsed = parse_occ_symbol(sym)
        if parsed is None:
            continue
        strike, expiry, is_call = parsed

        # DTE filter (use calendar days; floor at T_MIN later).
        dte_days = (expiry - now_utc).total_seconds() / 86400.0
        if dte_days < min_dte or dte_days > max_dte:
            continue

        oi_raw = opt.get("open_interest")
        if oi_raw is None or oi_raw <= 0:
            continue
        oi = int(oi_raw)

        iv = opt.get("iv")
        if iv is None:
            continue
        iv = float(iv)
        if not (IV_MIN <= iv <= IV_MAX):
            continue

        # T in years, with the 0DTE floor applied.
        T_years = max(dte_days / 365.0, T_MIN)

        contracts.append(Contract(
            strike=strike,
            expiry_utc=expiry,
            is_call=is_call,
            oi=oi,
            iv=iv,
            T=T_years,
        ))

    return spot, contracts


# ---------------------------------------------------------------------------
# Per-strike GEX (vectorized)
# ---------------------------------------------------------------------------

def _to_arrays(contracts: Iterable[Contract]) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Convert a list[Contract] to numpy arrays for vectorized math."""
    cs = list(contracts)
    K = np.fromiter((c.strike for c in cs), dtype=np.float64, count=len(cs))
    T = np.fromiter((c.T for c in cs), dtype=np.float64, count=len(cs))
    iv = np.fromiter((c.iv for c in cs), dtype=np.float64, count=len(cs))
    oi = np.fromiter((c.oi for c in cs), dtype=np.float64, count=len(cs))
    sign = np.fromiter((1.0 if c.is_call else -1.0 for c in cs), dtype=np.float64, count=len(cs))
    return K, T, iv, oi, sign


def compute_gex_per_contract(
    S: float,
    contracts: list[Contract],
    r: float,
    q: float,
) -> np.ndarray:
    """
    Compute the signed dollar GEX for each contract at spot S.

      GEX_i = Γ_i · OI_i · 100 · S² · 0.01 · sign_i

    Returns an ndarray aligned with the input list. Contracts with computed
    gamma > GAMMA_MAX get their GEX zeroed (sanity filter for 0DTE anomalies).
    """
    if not contracts:
        return np.zeros(0, dtype=np.float64)

    K, T, iv, oi, sign = _to_arrays(contracts)
    gamma = bs_gamma(S, K, T, r, q, iv)

    # Sanity filter — zero out contracts with anomalous gamma.
    gamma = np.where(gamma <= GAMMA_MAX, gamma, 0.0)

    per_contract = gamma * oi * CONTRACT_MULTIPLIER * (S * S) * 0.01 * sign
    return per_contract


def compute_gex_by_strike(
    S: float,
    contracts: list[Contract],
    r: float,
    q: float,
) -> dict[float, float]:
    """
    Aggregate per-contract GEX into a per-strike dictionary.

    Returns {strike: net_GEX_dollars_per_1pct_move}.
    """
    if not contracts:
        return {}

    per = compute_gex_per_contract(S, contracts, r, q)
    K = np.fromiter((c.strike for c in contracts), dtype=np.float64, count=len(contracts))

    # Group by strike. np.unique with return_inverse gives a fast aggregation.
    unique_strikes, inverse = np.unique(K, return_inverse=True)
    sums = np.zeros(unique_strikes.shape[0], dtype=np.float64)
    np.add.at(sums, inverse, per)

    return {float(k): float(v) for k, v in zip(unique_strikes, sums)}


def compute_oi_by_strike(contracts: list[Contract]) -> dict[float, int]:
    """Aggregate total OI per strike (calls + puts, unsigned)."""
    if not contracts:
        return {}

    K = np.fromiter((c.strike for c in contracts), dtype=np.float64, count=len(contracts))
    OI = np.fromiter((c.oi for c in contracts), dtype=np.float64, count=len(contracts))
    unique_strikes, inverse = np.unique(K, return_inverse=True)
    sums = np.zeros(unique_strikes.shape[0], dtype=np.float64)
    np.add.at(sums, inverse, OI)
    return {float(k): int(v) for k, v in zip(unique_strikes, sums)}


# ---------------------------------------------------------------------------
# Net GEX at hypothetical spot — for the flip sweep
# ---------------------------------------------------------------------------

def net_gex_at_spots(
    spots: np.ndarray,
    contracts: list[Contract],
    r: float,
    q: float,
) -> np.ndarray:
    """
    Evaluate total net GEX at each candidate spot in `spots`.

    For each candidate S', sums GEX_i(S') across all contracts. Used by
    find_gamma_flip to locate the zero-crossing.

    Implementation: we build the contract arrays once and broadcast against
    spots. With ~5000 contracts and ~400 spots, this is ~2M op evaluations
    in vectorized NumPy — runs in <500ms.
    """
    if not contracts:
        return np.zeros(spots.shape, dtype=np.float64)

    K, T, iv, oi, sign = _to_arrays(contracts)

    # Broadcast: spots[:, None] has shape (M, 1); contracts arrays are (N,).
    # Result has shape (M, N).
    S_grid = spots[:, None]  # (M, 1)
    K_b = K[None, :]
    T_b = T[None, :]
    iv_b = iv[None, :]

    gamma = bs_gamma(S_grid, K_b, T_b, r, q, iv_b)  # (M, N)
    gamma = np.where(gamma <= GAMMA_MAX, gamma, 0.0)

    # Per-contract GEX at each candidate spot.
    contrib = gamma * oi[None, :] * CONTRACT_MULTIPLIER * (S_grid ** 2) * 0.01 * sign[None, :]

    # Sum across contracts → total net GEX per candidate spot.
    return contrib.sum(axis=1)


def find_gamma_flip(
    S: float,
    contracts: list[Contract],
    r: float,
    q: float,
    sweep_pct: float = 0.10,
    sweep_step_pct: float = 0.0005,
) -> float | None:
    """
    Locate the gamma flip (zero-crossing of net GEX) nearest current spot.

    Sweeps candidate spots in [S·(1−sweep_pct), S·(1+sweep_pct)] at
    sweep_step_pct increments (default 0.05% step), evaluates total net GEX
    at each, and finds the nearest sign change. Linear interpolation between
    adjacent sweep points sharpens the estimate.

    Returns None if no zero-crossing exists in the sweep range.
    """
    if not contracts:
        return None

    spots = np.arange(
        S * (1.0 - sweep_pct),
        S * (1.0 + sweep_pct) + S * sweep_step_pct * 0.5,
        S * sweep_step_pct,
    )
    if spots.size < 2:
        return None

    totals = net_gex_at_spots(spots, contracts, r, q)
    signs = np.sign(totals)

    # Find indices where sign changes (skipping zero entries).
    nonzero = signs != 0
    if not np.any(nonzero):
        return None

    diffs = np.diff(signs)
    crossings = np.where(diffs != 0)[0]
    if crossings.size == 0:
        return None

    # Pick the crossing whose midpoint is nearest current spot.
    midpoints = (spots[crossings] + spots[crossings + 1]) / 2
    nearest = crossings[np.argmin(np.abs(midpoints - S))]

    # Linear interpolation between (spots[nearest], totals[nearest])
    # and (spots[nearest+1], totals[nearest+1]).
    a, b = spots[nearest], spots[nearest + 1]
    ya, yb = totals[nearest], totals[nearest + 1]
    if yb == ya:
        return float((a + b) / 2)
    flip = a - ya * (b - a) / (yb - ya)
    return float(flip)


# ---------------------------------------------------------------------------
# Walls & clusters
# ---------------------------------------------------------------------------

def identify_walls_clusters(
    gex_by_strike: dict[float, float],
    oi_by_strike: dict[float, int],
    top_n_clusters: int = 5,
    top_n_oi: int = 5,
) -> dict:
    """
    Identify the Call Wall, Put Wall, and top-N positive/negative GEX
    clusters and top-N OI clusters.

    Returns a dict with these keys:
      - call_wall: {"strike": K, "gex_dollars": v}  (most positive)
      - put_wall:  {"strike": K, "gex_dollars": v}  (most negative)
      - top_pos_clusters: list[dict] sorted by GEX desc, excluding call_wall
      - top_neg_clusters: list[dict] sorted by GEX asc, excluding put_wall
      - top_oi_clusters: list[dict] sorted by OI desc
      - total_net_gex: float
    """
    if not gex_by_strike:
        return {
            "call_wall": None,
            "put_wall": None,
            "top_pos_clusters": [],
            "top_neg_clusters": [],
            "top_oi_clusters": [],
            "total_net_gex": 0.0,
        }

    items = list(gex_by_strike.items())

    pos = [(k, v) for k, v in items if v > 0]
    neg = [(k, v) for k, v in items if v < 0]

    call_wall = max(pos, key=lambda kv: kv[1], default=None)
    put_wall = min(neg, key=lambda kv: kv[1], default=None)

    pos_sorted = sorted(pos, key=lambda kv: kv[1], reverse=True)
    neg_sorted = sorted(neg, key=lambda kv: kv[1])  # most negative first

    # Exclude the wall from clusters.
    pos_clusters = [
        {"strike": k, "gex_dollars": v}
        for k, v in pos_sorted
        if call_wall is None or k != call_wall[0]
    ][:top_n_clusters]

    neg_clusters = [
        {"strike": k, "gex_dollars": v}
        for k, v in neg_sorted
        if put_wall is None or k != put_wall[0]
    ][:top_n_clusters]

    oi_sorted = sorted(oi_by_strike.items(), key=lambda kv: kv[1], reverse=True)
    oi_clusters = [
        {"strike": k, "open_interest": v}
        for k, v in oi_sorted[:top_n_oi]
    ]

    return {
        "call_wall": {"strike": float(call_wall[0]), "gex_dollars": float(call_wall[1])} if call_wall else None,
        "put_wall": {"strike": float(put_wall[0]), "gex_dollars": float(put_wall[1])} if put_wall else None,
        "top_pos_clusters": pos_clusters,
        "top_neg_clusters": neg_clusters,
        "top_oi_clusters": oi_clusters,
        "total_net_gex": float(sum(gex_by_strike.values())),
    }
