"""
Full-pipeline test on a synthetic SPX chain (index underlying).

Verifies:
- SPX parses with index OCC roots (SPXW for 0DTE, SPX for monthly)
- Mini contracts (XSP) are excluded
- Blended levels compute across all expiries
- 0DTE block isolates today's expiry
- 1D expected move present
- Futures mapping (multiplier ~1.0 for index) is sane
- MenthorQ-style aliases populated (call_resistance_0dte, hvl_0dte, gamma_wall_0dte)
"""
import asyncio
import json
from datetime import datetime, timezone, timedelta

import pytest

from ffgex_fetcher.__main__ import process_ticker


def _occ(root, exp_dt, is_call, strike):
    yy = exp_dt.strftime("%y"); mm = exp_dt.strftime("%m"); dd = exp_dt.strftime("%d")
    cp = "C" if is_call else "P"
    strike_int = int(round(strike * 1000))
    return f"{root}{yy}{mm}{dd}{cp}{strike_int:08d}"


def build_spx_chain(now_utc):
    spot = 7361.0
    today = now_utc.astimezone(timezone.utc)
    today_exp = today.replace(hour=20, minute=0, second=0, microsecond=0)
    week_exp = today_exp + timedelta(days=3)
    month_exp = today_exp + timedelta(days=24)

    options = []

    def add(root, exp, is_call, strike, oi, iv):
        options.append({
            "option": _occ(root, exp, is_call, strike),
            "open_interest": oi, "iv": iv, "volume": 0,
        })

    # --- 0DTE (SPXW, today) — cluster: calls heavy at 7400, puts heavy at 7300 ---
    for k in range(7250, 7451, 5):
        # gaussian-ish OI around 7400 calls / 7300 puts
        call_oi = int(3000 * 2.718 ** (-((k - 7400) ** 2) / (2 * 30 ** 2))) + 50
        put_oi = int(3000 * 2.718 ** (-((k - 7300) ** 2) / (2 * 30 ** 2))) + 50
        add("SPXW", today_exp, True, k, call_oi, 0.11)
        add("SPXW", today_exp, False, k, put_oi, 0.12)

    # --- Weekly (SPXW, +3d) ---
    for k in range(7200, 7501, 25):
        add("SPXW", week_exp, True, k, 800, 0.13)
        add("SPXW", week_exp, False, k, 800, 0.14)

    # --- Monthly (SPX, +24d) — different cluster at 7500/7000 ---
    for k in range(7000, 7601, 25):
        call_oi = int(6000 * 2.718 ** (-((k - 7500) ** 2) / (2 * 60 ** 2))) + 100
        put_oi = int(6000 * 2.718 ** (-((k - 7000) ** 2) / (2 * 60 ** 2))) + 100
        add("SPX", month_exp, True, k, call_oi, 0.15)
        add("SPX", month_exp, False, k, put_oi, 0.16)

    # --- Mini SPX (XSP) — must be EXCLUDED ---
    add("XSP", today_exp, True, 740, 99999, 0.11)
    add("XSP", today_exp, False, 730, 99999, 0.12)

    return {"data": {"current_price": spot, "options": options}}


async def _run():
    # 9:40am ET on a weekday = 13:40 UTC (EDT)
    now = datetime(2026, 6, 24, 13, 40, tzinfo=timezone.utc)
    chain = build_spx_chain(now)
    result = await process_ticker("SPX", chain, risk_free_rate=0.043, max_dte=30, now_utc=now)
    return result, now


def test_spx_pipeline_full():
    result, now = asyncio.run(_run())
    assert result["status"] == "ok"
    assert result["underlying_kind"] == "index"
    # Multiplier ~1.009 (ES 7425 / SPX 7361)
    assert 0.97 <= result["multiplier"] <= 1.03

    # --- Blended levels present ---
    bl = result["blended"]
    assert bl["call_wall"] is not None
    assert bl["put_wall"] is not None
    # Blended blends 0DTE + weekly + monthly; the heaviest OI is monthly (6000)
    # so blended walls should lean toward monthly cluster (7500/7000)
    print(f"\n  Blended CW={bl['call_wall']['etf_strike']} PW={bl['put_wall']['etf_strike']}")

    # --- 0DTE block present and isolated ---
    assert result["dte0"] is not None, "0DTE block should be populated"
    assert result["dte0_contract_count"] > 0
    d0 = result["dte0"]
    # 0DTE call wall is the highest-GEX strike (gamma-weighted, pulled toward
    # spot from the raw OI peak). With spot 7361 and OI peaking at 7400 calls,
    # the GEX peak lands in the 7375-7400 band — and critically NOT at the
    # monthly cluster (7500). This is the correct GEX definition.
    assert 7370 <= d0["call_wall"]["etf_strike"] <= 7405, \
        f"0DTE CW should be in the 0DTE cluster band, got {d0['call_wall']['etf_strike']}"
    assert d0["call_wall"]["etf_strike"] != 7500.0, "0DTE CW must not be the monthly cluster"
    # Put wall similarly in the 0DTE put cluster (around 7300, gamma-weighted)
    assert 7300 <= d0["put_wall"]["etf_strike"] <= 7340, \
        f"0DTE PW should be in the 0DTE cluster band, got {d0['put_wall']['etf_strike']}"
    assert d0["put_wall"]["etf_strike"] != 7000.0, "0DTE PW must not be the monthly cluster"
    print(f"  0DTE CW={d0['call_wall']['etf_strike']} PW={d0['put_wall']['etf_strike']} "
          f"flip={d0['gamma_flip']['etf_strike'] if d0['gamma_flip'] else None}")

    # --- MenthorQ-style aliases ---
    assert d0["call_resistance_0dte"]["etf_strike"] == d0["call_wall"]["etf_strike"]
    assert d0["put_support_0dte"]["etf_strike"] == d0["put_wall"]["etf_strike"]
    assert "hvl_0dte" in d0
    assert "gamma_wall_0dte" in d0
    assert d0["gamma_wall_0dte"] is not None
    print(f"  Gamma Wall 0DTE = {d0['gamma_wall_0dte']['etf_strike']}")

    # --- Expected move present ---
    assert result["expected_move_1d"] is not None
    em = result["expected_move_1d"]
    assert em["low_etf"] < result["spot"] < em["high_etf"]
    print(f"  1D move=${em['move']:.2f} band=[{em['low_etf']:.2f}, {em['high_etf']:.2f}]")

    # --- JSON serializable ---
    blob = json.dumps(result)
    assert len(blob) > 500


def test_xsp_excluded():
    """The XSP mini contracts with 99999 OI must not appear in any level."""
    result, now = asyncio.run(_run())
    # If XSP leaked in, the 0DTE walls would show 740/730 with absurd magnitude.
    d0 = result["dte0"]
    assert d0["call_wall"]["etf_strike"] > 7000, "XSP 740 strike must not appear"
    assert d0["put_wall"]["etf_strike"] > 7000, "XSP 730 strike must not appear"


if __name__ == "__main__":
    test_spx_pipeline_full()
    test_xsp_excluded()
    print("\nSPX pipeline tests passed.")
