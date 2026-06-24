"""
Validate 0DTE contract selection (Option A: strict same-day expiry).

Critical cases:
1. A contract expiring at 4pm ET today IS 0DTE when the run is at 9:35am ET.
2. A contract expiring tomorrow is NOT 0DTE.
3. Timezone correctness: the "today" boundary uses US Eastern, not UTC.
   (A run at 8pm ET = 00:00 UTC next day must still treat ET-today as today.)
4. The 0DTE walls/flip isolate from longer-dated noise.
"""
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import pytest

from ffgex_fetcher.gex_engine import (
    Contract,
    select_0dte_contracts,
    compute_gex_by_strike,
    compute_oi_by_strike,
    identify_walls_clusters,
    find_gamma_flip,
    compute_expected_move_1d,
)

ET = ZoneInfo("America/New_York")


def _mk(strike, expiry_utc, is_call, oi, iv=0.15, T=0.01):
    return Contract(strike=strike, expiry_utc=expiry_utc, is_call=is_call, oi=oi, iv=iv, T=T)


def test_isolates_today_expiry_during_market_hours():
    # Run at 9:35am ET on 2026-06-24 (= 13:35 UTC).
    run = datetime(2026, 6, 24, 13, 35, tzinfo=timezone.utc)
    # Today's expiry: 4pm ET = 20:00 UTC same day.
    today_exp = datetime(2026, 6, 24, 20, 0, tzinfo=timezone.utc)
    # Tomorrow's expiry.
    tmrw_exp = datetime(2026, 6, 25, 20, 0, tzinfo=timezone.utc)
    # Next week.
    nextwk_exp = datetime(2026, 7, 1, 20, 0, tzinfo=timezone.utc)

    contracts = [
        _mk(7400, today_exp, True, 100),
        _mk(7400, today_exp, False, 100),
        _mk(7400, tmrw_exp, True, 999),
        _mk(7400, nextwk_exp, True, 999),
    ]
    dte0 = select_0dte_contracts(contracts, now_utc=run)
    assert len(dte0) == 2, f"Expected 2 same-day contracts, got {len(dte0)}"
    for c in dte0:
        assert c.expiry_utc == today_exp


def test_timezone_boundary_evening_run():
    """
    Run at 8:00pm ET on 2026-06-24. In UTC that's 00:00 on 2026-06-25.
    A contract expiring 4pm ET 2026-06-24 (20:00 UTC 06-24) must STILL be
    treated as 'today' (ET date 06-24), even though UTC has rolled to 06-25.
    """
    # 8pm ET = 00:00 UTC next day
    run = datetime(2026, 6, 25, 0, 0, tzinfo=timezone.utc)
    assert run.astimezone(ET).date() == datetime(2026, 6, 24).date()  # sanity

    today_exp = datetime(2026, 6, 24, 20, 0, tzinfo=timezone.utc)  # 4pm ET 06-24
    contracts = [_mk(7400, today_exp, True, 100)]
    dte0 = select_0dte_contracts(contracts, now_utc=run)
    # Expiry already passed (4pm) but it's still the same ET trading date.
    assert len(dte0) == 1, "Same ET-date expiry should count as 0DTE"


def test_no_expiry_today_returns_empty():
    """A ticker whose nearest expiry is days away yields no 0DTE."""
    run = datetime(2026, 6, 24, 13, 35, tzinfo=timezone.utc)
    friday_exp = datetime(2026, 6, 26, 20, 0, tzinfo=timezone.utc)
    contracts = [_mk(300, friday_exp, True, 500), _mk(290, friday_exp, False, 500)]
    dte0 = select_0dte_contracts(contracts, now_utc=run)
    assert dte0 == []


def test_0dte_walls_isolate_from_longdated():
    """
    Build a chain where 0DTE has its OWN wall structure distinct from the
    longer-dated contracts. Confirm the 0DTE walls reflect only same-day OI.
    """
    run = datetime(2026, 6, 24, 13, 35, tzinfo=timezone.utc)
    today_exp = datetime(2026, 6, 24, 20, 0, tzinfo=timezone.utc)
    month_exp = datetime(2026, 7, 17, 20, 0, tzinfo=timezone.utc)
    spot = 7400.0

    # 0DTE: heavy calls at 7450, heavy puts at 7350
    # Monthly: heavy calls at 7600, heavy puts at 7000 (different strikes)
    contracts = [
        # 0DTE cluster
        _mk(7450, today_exp, True, 5000, T=0.0014),
        _mk(7350, today_exp, False, 5000, T=0.0014),
        _mk(7400, today_exp, True, 1000, T=0.0014),
        _mk(7400, today_exp, False, 1000, T=0.0014),
        # Monthly cluster (should NOT appear in 0DTE walls)
        _mk(7600, month_exp, True, 9000, T=0.063),
        _mk(7000, month_exp, False, 9000, T=0.063),
    ]

    dte0 = select_0dte_contracts(contracts, now_utc=run)
    assert len(dte0) == 4

    gex = compute_gex_by_strike(spot, dte0, r=0.043, q=0.0125)
    oi = compute_oi_by_strike(dte0)
    walls = identify_walls_clusters(gex, oi, top_n_clusters=5, top_n_oi=5)

    # 0DTE call wall should be 7450 (not the monthly 7600)
    assert walls["call_wall"] is not None
    assert walls["call_wall"]["strike"] == 7450.0, \
        f"0DTE call wall should be 7450, got {walls['call_wall']['strike']}"
    # 0DTE put wall should be 7350 (not the monthly 7000)
    assert walls["put_wall"] is not None
    assert walls["put_wall"]["strike"] == 7350.0, \
        f"0DTE put wall should be 7350, got {walls['put_wall']['strike']}"


def test_expected_move_1d():
    """1D expected move from ATM IV."""
    run = datetime(2026, 6, 24, 13, 35, tzinfo=timezone.utc)
    today_exp = datetime(2026, 6, 24, 20, 0, tzinfo=timezone.utc)
    spot = 7400.0
    # ATM contract at 7400 with 12% IV
    contracts = [
        _mk(7400, today_exp, True, 1000, iv=0.12, T=0.0014),
        _mk(7500, today_exp, True, 500, iv=0.13, T=0.0014),
        _mk(7300, today_exp, False, 500, iv=0.14, T=0.0014),
    ]
    move, atm_iv = compute_expected_move_1d(spot, contracts)
    assert atm_iv == 0.12, f"ATM IV should be from the 7400 strike: {atm_iv}"
    # move = 7400 * 0.12 * sqrt(1/252)
    import math
    expected = 7400 * 0.12 * math.sqrt(1/252)
    assert abs(move - expected) < 0.01, f"Expected move {expected}, got {move}"
    print(f"\n  1D expected move: ${move:.2f} (band: {spot-move:.2f} - {spot+move:.2f})")


if __name__ == "__main__":
    test_isolates_today_expiry_during_market_hours()
    test_timezone_boundary_evening_run()
    test_no_expiry_today_returns_empty()
    test_0dte_walls_isolate_from_longdated()
    test_expected_move_1d()
    print("All 0DTE selection tests passed.")
