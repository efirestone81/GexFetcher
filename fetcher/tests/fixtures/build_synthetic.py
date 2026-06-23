"""
Generate a realistic synthetic SPY chain fixture that mimics the structure
of the CBOE CDN response, with plausible OI/IV distributions across strikes
and expirations.

Used to validate the full pipeline when the live CBOE endpoint is unreachable
(e.g. in the sandbox where the host is not in the allowlist).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np


REF_NOW = datetime(2026, 5, 23, 14, 0, tzinfo=timezone.utc)
SPOT = 583.42

# Expiries: 0DTE, +1DTE, +2DTE, weekly, +14, +21, +28
EXPIRIES_DAYS = [0, 1, 2, 7, 14, 21, 28]


def occ(root: str, expiry: datetime, is_call: bool, strike_dollars: float) -> str:
    yy = expiry.year % 100
    cp = "C" if is_call else "P"
    # Strike encoded as 8-digit integer with 3 implied decimals
    return f"{root}{yy:02d}{expiry.month:02d}{expiry.day:02d}{cp}{int(round(strike_dollars * 1000)):08d}"


def build_synthetic_spy_chain() -> dict:
    rng = np.random.default_rng(20260523)
    options = []

    # Strikes: every $1 from $560 to $610 (roughly ±5% of spot)
    strikes = list(range(560, 611))

    # Add some "junk" contracts that should be filtered:
    # 1. A contract with OI=0 (illiquid)
    # 2. A contract with IV=0 (no market quote)
    # 3. A contract with IV=10 (anomalous, above clamp)
    # 4. A contract with a far-future expiry beyond max_dte
    # 5. A contract with a past expiry (DTE < -1)
    far_future = (REF_NOW + timedelta(days=180)).replace(hour=20, minute=0, second=0)
    way_past = (REF_NOW - timedelta(days=10)).replace(hour=20, minute=0, second=0)
    options.extend([
        {"option": occ("SPY", far_future, True, 600.0),
         "open_interest": 5000, "iv": 0.20, "volume": 0},
        {"option": occ("SPY", way_past, True, 580.0),
         "open_interest": 5000, "iv": 0.20, "volume": 0},
        {"option": occ("SPY", REF_NOW + timedelta(days=7), True, 600.0),
         "open_interest": 0, "iv": 0.20, "volume": 0},       # OI=0
        {"option": occ("SPY", REF_NOW + timedelta(days=7), True, 605.0),
         "open_interest": 100, "iv": 0.0, "volume": 0},      # IV=0
        {"option": occ("SPY", REF_NOW + timedelta(days=7), True, 610.0),
         "open_interest": 100, "iv": 10.0, "volume": 0},     # IV anomaly
        {"option": "GARBAGE_SYMBOL", "open_interest": 100, "iv": 0.20, "volume": 0},
    ])

    for dte in EXPIRIES_DAYS:
        exp = (REF_NOW + timedelta(days=dte)).replace(hour=20, minute=0, second=0)
        for K in strikes:
            # Base OI: heavier near ATM, falls off OTM
            moneyness = abs(K - SPOT) / SPOT
            base_oi_call = max(20, int(15000 * np.exp(-30 * moneyness) + rng.integers(0, 500)))
            base_oi_put = max(20, int(15000 * np.exp(-30 * moneyness) + rng.integers(0, 500)))

            # Inject heavier OI at "round" strikes ($5 multiples)
            if K % 5 == 0:
                base_oi_call = int(base_oi_call * 1.5)
                base_oi_put = int(base_oi_put * 1.5)

            # Inject a heavy call wall at K=590 (above spot)
            if K == 590:
                base_oi_call = int(base_oi_call * 5)
            # Inject a heavy put wall at K=575 (below spot)
            if K == 575:
                base_oi_put = int(base_oi_put * 5)

            # Weight by DTE — near-term expiries get more OI
            dte_weight = 1.0 if dte <= 2 else (0.6 if dte <= 14 else 0.3)
            base_oi_call = int(base_oi_call * dte_weight)
            base_oi_put = int(base_oi_put * dte_weight)

            # IV — smile shape, higher OTM puts (skew)
            atm_iv = 0.12 + 0.02 * np.sqrt(max(dte, 1) / 30)  # term structure
            otm_call_extra = 0.0006 * max(0, K - SPOT)
            otm_put_extra = 0.0010 * max(0, SPOT - K)
            iv_call = float(atm_iv + otm_call_extra + rng.normal(0, 0.003))
            iv_put = float(atm_iv + otm_put_extra + rng.normal(0, 0.003))
            iv_call = max(0.05, min(2.5, iv_call))
            iv_put = max(0.05, min(2.5, iv_put))

            options.append({
                "option": occ("SPY", exp, True, float(K)),
                "open_interest": base_oi_call,
                "iv": round(iv_call, 4),
                "volume": int(base_oi_call * 0.3),
                "bid": 0.0,
                "ask": 0.0,
            })
            options.append({
                "option": occ("SPY", exp, False, float(K)),
                "open_interest": base_oi_put,
                "iv": round(iv_put, 4),
                "volume": int(base_oi_put * 0.3),
                "bid": 0.0,
                "ask": 0.0,
            })

    return {
        "data": {
            "symbol": "SPY",
            "current_price": SPOT,
            "options": options,
            "_synthetic": True,
            "_synthetic_ref_time_utc": REF_NOW.isoformat(),
        }
    }


def main():
    out_path = Path(__file__).parent / "spy_chain_synthetic.json"
    chain = build_synthetic_spy_chain()
    out_path.write_text(json.dumps(chain, separators=(",", ":")))
    n_opts = len(chain["data"]["options"])
    print(f"Wrote {n_opts:,} synthetic SPY options to {out_path}")
    print(f"Spot: ${SPOT}")
    print(f"Expiries: {EXPIRIES_DAYS} days")
    print(f"Strikes: 560..610 ($1 spacing)")
    print(f"Embedded heavy call OI at K=590, heavy put OI at K=575")


if __name__ == "__main__":
    main()
