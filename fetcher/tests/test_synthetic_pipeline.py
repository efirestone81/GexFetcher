"""
Run the full pipeline against the synthetic SPY chain fixture and verify
the outputs are sane.

This is the validation gate for the math + parsing pipeline before we wire
up the orchestrator. By construction we know:
  - Heavy call OI at K=590 → Call Wall should be at $590
  - Heavy put OI at K=575 → Put Wall should be at $575
  - The flip should land between them, near spot ($583.42)
  - Total contracts after filter should be in the hundreds
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ffgex_fetcher.gex_engine import (
    parse_chain, compute_gex_by_strike, compute_oi_by_strike,
    find_gamma_flip, identify_walls_clusters,
)
from ffgex_fetcher.futures_mapper import DEFAULT_TICKERS, compute_multiplier, compute_carry_basis, map_strike


REF_NOW = datetime(2026, 5, 23, 14, 0, tzinfo=timezone.utc)
FIXTURE = Path(__file__).parent / "fixtures" / "spy_chain_synthetic.json"


@pytest.fixture(scope="module")
def chain():
    return json.loads(FIXTURE.read_text())


def test_synthetic_fixture_loads(chain):
    assert chain["data"]["symbol"] == "SPY"
    assert chain["data"]["current_price"] == 583.42
    assert len(chain["data"]["options"]) > 500


def test_synthetic_full_pipeline(chain, capsys):
    cfg = DEFAULT_TICKERS["SPY"]
    r = 0.043
    q = cfg.dividend_yield

    spot, contracts = parse_chain(chain, now_utc=REF_NOW, max_dte=30)
    assert spot == 583.42
    assert len(contracts) > 100, f"Too few contracts: {len(contracts)}"

    gex_map = compute_gex_by_strike(spot, contracts, r=r, q=q)
    oi_map = compute_oi_by_strike(contracts)
    walls = identify_walls_clusters(gex_map, oi_map, top_n_clusters=5, top_n_oi=5)
    flip = find_gamma_flip(spot, contracts, r=r, q=q)

    # ---- The structural expectations ----
    assert walls["call_wall"]["strike"] == 590.0, \
        f"Expected call wall at 590, got {walls['call_wall']['strike']}"
    assert walls["put_wall"]["strike"] == 575.0, \
        f"Expected put wall at 575, got {walls['put_wall']['strike']}"

    # Flip should land between PW (575) and CW (590), near spot (583.42).
    assert flip is not None
    assert 575.0 < flip < 590.0, f"Flip {flip} outside [575, 590]"
    assert 580.0 < flip < 587.0, f"Flip {flip} far from spot 583.42"

    # ---- Magnitude sanity ----
    cw_gex = walls["call_wall"]["gex_dollars"]
    pw_gex = walls["put_wall"]["gex_dollars"]
    assert cw_gex > 0
    assert pw_gex < 0
    # Walls should each be ~ $100M–$10B for synthetic SPY data.
    assert 1e7 < abs(cw_gex) < 1e11, f"CW GEX out of range: {cw_gex:.2e}"
    assert 1e7 < abs(pw_gex) < 1e11, f"PW GEX out of range: {pw_gex:.2e}"

    # ---- Futures mapping ----
    multiplier, m_warnings = compute_multiplier(spot, cfg)
    basis = compute_carry_basis(spot, r, cfg, T_years=0.083)  # 1 month
    # SPY $583.42 with ES_ref=$5800 → multiplier ≈ 9.94
    assert 9.5 < multiplier < 10.5

    cw_mapped = map_strike(walls["call_wall"]["strike"], multiplier, basis, cfg)
    pw_mapped = map_strike(walls["put_wall"]["strike"], multiplier, basis, cfg)
    print(f"\n--- Pipeline output ---")
    print(f"Spot: ${spot} ETF → ${spot * multiplier:.2f} ES (mult={multiplier:.4f})")
    print(f"Call Wall: ${walls['call_wall']['strike']} ETF → "
          f"ES mult ${cw_mapped['futures_mult']:.2f} | basis ${cw_mapped['futures_basis']:.2f}  "
          f"(${cw_gex/1e9:+.2f}B)")
    print(f"Put Wall:  ${walls['put_wall']['strike']} ETF → "
          f"ES mult ${pw_mapped['futures_mult']:.2f} | basis ${pw_mapped['futures_basis']:.2f}  "
          f"(${pw_gex/1e9:+.2f}B)")
    print(f"Flip: ${flip:.2f} ETF → ES mult ${flip * multiplier:.2f} | "
          f"basis ${flip * cfg.scale_to_index + basis:.2f}")
    print(f"Total Net GEX: ${walls['total_net_gex']/1e9:+.2f}B")

    print(f"\nTop +GEX clusters (excluding CW):")
    for c in walls["top_pos_clusters"][:3]:
        m = map_strike(c["strike"], multiplier, basis, cfg)
        print(f"  ETF ${c['strike']:.2f} → ES ${m['futures_mult']:.2f}  ${c['gex_dollars']/1e9:+.2f}B")
    print(f"Top -GEX clusters (excluding PW):")
    for c in walls["top_neg_clusters"][:3]:
        m = map_strike(c["strike"], multiplier, basis, cfg)
        print(f"  ETF ${c['strike']:.2f} → ES ${m['futures_mult']:.2f}  ${c['gex_dollars']/1e9:+.2f}B")
    print(f"Top OI clusters:")
    for c in walls["top_oi_clusters"][:3]:
        m = map_strike(c["strike"], multiplier, basis, cfg)
        print(f"  ETF ${c['strike']:.2f} → ES ${m['futures_mult']:.2f}  OI={c['open_interest']:,}")
