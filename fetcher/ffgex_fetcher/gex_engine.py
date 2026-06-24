"""GEX engine — parse, aggregate, walls, flip."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

import numpy as np

from ffgex_fetcher.greeks import bs_gamma, IV_MIN, IV_MAX, T_MIN

CONTRACT_MULTIPLIER = 100
GAMMA_MAX = 2.0

_OCC_RE = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")

# Roots we EXCLUDE — mini/reduced-value contracts that would distort GEX
# because their multiplier differs from the standard 100.
#   XSP  = mini-SPX (1/10 SPX)
#   NQX  = mini-NDX (1/40 NDX-ish reduced value)
#   MRUT = mini-Russell, etc.
EXCLUDED_ROOTS = frozenset({"XSP", "NQX", "MRUT", "NANOS"})


@dataclass(frozen=True, slots=True)
class Contract:
    strike: float
    expiry_utc: datetime
    is_call: bool
    oi: int
    iv: float
    T: float


def parse_occ_symbol(symbol: str):
    m = _OCC_RE.match(symbol)
    if not m:
        return None
    root, yymmdd, cp, strike_raw = m.groups()
    try:
        yy = int(yymmdd[:2]); mm = int(yymmdd[2:4]); dd = int(yymmdd[4:6])
        expiry = datetime(2000 + yy, mm, dd, 20, 0, tzinfo=timezone.utc)
    except ValueError:
        return None
    strike = int(strike_raw) / 1000.0
    return root, expiry, (cp == "C"), strike


def parse_chain(chain_json, now_utc=None, max_dte=30, min_dte=-1):
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
    excluded_mini = 0

    for opt in raw_options:
        sym = opt.get("option")
        if not isinstance(sym, str):
            continue
        parsed = parse_occ_symbol(sym)
        if parsed is None:
            continue
        root, expiry, is_call, strike = parsed

        # Exclude mini / reduced-value contracts by root.
        if root in EXCLUDED_ROOTS:
            excluded_mini += 1
            continue

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

        T_years = max(dte_days / 365.0, T_MIN)
        contracts.append(Contract(strike, expiry, is_call, oi, iv, T_years))

    return spot, contracts


def select_0dte_contracts(contracts, now_utc=None, market_tz="America/New_York"):
    """
    Return the subset of contracts that expire on the CURRENT trading date
    (strict 0DTE — Option A: 0 calendar days in the market's local timezone).

    "Today" is defined by the market's local date (US Eastern for SPX/NDX), not
    UTC, because a contract expiring at 4pm ET today is 0DTE even though that
    instant is 20:00 UTC. The expiry datetimes are stored at 20:00 UTC (the SPX
    PM-settlement instant during DST); we compare local dates.

    On an actual expiration day (SPX/NDX have these every weekday), this isolates
    the same-day options. On a ticker with no expiry today, this returns [].
    """
    from zoneinfo import ZoneInfo

    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")

    tz = ZoneInfo(market_tz)
    today_local = now_utc.astimezone(tz).date()

    out = []
    for c in contracts:
        exp_local_date = c.expiry_utc.astimezone(tz).date()
        if exp_local_date == today_local:
            out.append(c)
    return out


def compute_expected_move_1d(spot, contracts, trading_days_per_year=252):
    """
    1-day expected move from ATM implied volatility.

        1D move = spot * ATM_IV * sqrt(1 / trading_days_per_year)

    ATM_IV is taken from the contract whose strike is closest to spot (using the
    nearest-expiry contracts available in the input). Returns (move, atm_iv) or
    (None, None) if no contracts.

    This doubles as the 1D Max / 1D Min band (spot +/- move) and as the filter
    window for ranked secondary levels.
    """
    if not contracts:
        return None, None
    # Find the contract with strike closest to spot; prefer nearest expiry.
    nearest_T = min(c.T for c in contracts)
    near_exp = [c for c in contracts if abs(c.T - nearest_T) < 1e-9]
    atm = min(near_exp, key=lambda c: abs(c.strike - spot))
    atm_iv = atm.iv
    move = spot * atm_iv * math.sqrt(1.0 / trading_days_per_year)
    return float(move), float(atm_iv)


def _to_arrays(contracts: Iterable[Contract]):
    cs = list(contracts)
    K = np.fromiter((c.strike for c in cs), dtype=np.float64, count=len(cs))
    T = np.fromiter((c.T for c in cs), dtype=np.float64, count=len(cs))
    iv = np.fromiter((c.iv for c in cs), dtype=np.float64, count=len(cs))
    oi = np.fromiter((c.oi for c in cs), dtype=np.float64, count=len(cs))
    sign = np.fromiter((1.0 if c.is_call else -1.0 for c in cs), dtype=np.float64, count=len(cs))
    return K, T, iv, oi, sign


def compute_gex_per_contract(S, contracts, r, q):
    if not contracts:
        return np.zeros(0, dtype=np.float64)
    K, T, iv, oi, sign = _to_arrays(contracts)
    gamma = bs_gamma(S, K, T, r, q, iv)
    gamma = np.where(gamma <= GAMMA_MAX, gamma, 0.0)
    return gamma * oi * CONTRACT_MULTIPLIER * (S * S) * 0.01 * sign


def compute_gex_by_strike(S, contracts, r, q):
    if not contracts:
        return {}
    per = compute_gex_per_contract(S, contracts, r, q)
    K = np.fromiter((c.strike for c in contracts), dtype=np.float64, count=len(contracts))
    unique_strikes, inverse = np.unique(K, return_inverse=True)
    sums = np.zeros(unique_strikes.shape[0], dtype=np.float64)
    np.add.at(sums, inverse, per)
    return {float(k): float(v) for k, v in zip(unique_strikes, sums)}


def compute_oi_by_strike(contracts):
    if not contracts:
        return {}
    K = np.fromiter((c.strike for c in contracts), dtype=np.float64, count=len(contracts))
    OI = np.fromiter((c.oi for c in contracts), dtype=np.float64, count=len(contracts))
    unique_strikes, inverse = np.unique(K, return_inverse=True)
    sums = np.zeros(unique_strikes.shape[0], dtype=np.float64)
    np.add.at(sums, inverse, OI)
    return {float(k): int(v) for k, v in zip(unique_strikes, sums)}


def net_gex_at_spots(spots, contracts, r, q):
    if not contracts:
        return np.zeros(spots.shape, dtype=np.float64)
    K, T, iv, oi, sign = _to_arrays(contracts)
    S_grid = spots[:, None]
    gamma = bs_gamma(S_grid, K[None, :], T[None, :], r, q, iv[None, :])
    gamma = np.where(gamma <= GAMMA_MAX, gamma, 0.0)
    contrib = gamma * oi[None, :] * CONTRACT_MULTIPLIER * (S_grid ** 2) * 0.01 * sign[None, :]
    return contrib.sum(axis=1)


def find_gamma_flip(S, contracts, r, q, sweep_pct=0.10, sweep_step_pct=0.0005):
    if not contracts:
        return None
    spots = np.arange(S * (1 - sweep_pct), S * (1 + sweep_pct) + S * sweep_step_pct * 0.5, S * sweep_step_pct)
    if spots.size < 2:
        return None
    totals = net_gex_at_spots(spots, contracts, r, q)
    signs = np.sign(totals)
    if not np.any(signs != 0):
        return None
    crossings = np.where(np.diff(signs) != 0)[0]
    if crossings.size == 0:
        return None
    midpoints = (spots[crossings] + spots[crossings + 1]) / 2
    nearest = crossings[np.argmin(np.abs(midpoints - S))]
    a, b = spots[nearest], spots[nearest + 1]
    ya, yb = totals[nearest], totals[nearest + 1]
    if yb == ya:
        return float((a + b) / 2)
    return float(a - ya * (b - a) / (yb - ya))


def identify_walls_clusters(gex_by_strike, oi_by_strike, top_n_clusters=5, top_n_oi=5):
    if not gex_by_strike:
        return {"call_wall": None, "put_wall": None, "top_pos_clusters": [],
                "top_neg_clusters": [], "top_oi_clusters": [], "total_net_gex": 0.0}
    items = list(gex_by_strike.items())
    pos = [(k, v) for k, v in items if v > 0]
    neg = [(k, v) for k, v in items if v < 0]
    call_wall = max(pos, key=lambda kv: kv[1], default=None)
    put_wall = min(neg, key=lambda kv: kv[1], default=None)
    pos_sorted = sorted(pos, key=lambda kv: kv[1], reverse=True)
    neg_sorted = sorted(neg, key=lambda kv: kv[1])
    pos_clusters = [{"strike": k, "gex_dollars": v} for k, v in pos_sorted
                    if call_wall is None or k != call_wall[0]][:top_n_clusters]
    neg_clusters = [{"strike": k, "gex_dollars": v} for k, v in neg_sorted
                    if put_wall is None or k != put_wall[0]][:top_n_clusters]
    oi_sorted = sorted(oi_by_strike.items(), key=lambda kv: kv[1], reverse=True)
    oi_clusters = [{"strike": k, "open_interest": v} for k, v in oi_sorted[:top_n_oi]]
    return {
        "call_wall": {"strike": float(call_wall[0]), "gex_dollars": float(call_wall[1])} if call_wall else None,
        "put_wall": {"strike": float(put_wall[0]), "gex_dollars": float(put_wall[1])} if put_wall else None,
        "top_pos_clusters": pos_clusters,
        "top_neg_clusters": neg_clusters,
        "top_oi_clusters": oi_clusters,
        "total_net_gex": float(sum(gex_by_strike.values())),
    }
