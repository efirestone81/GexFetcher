"""
End-to-end orchestrator test.

Bypasses the live CBOE fetch by injecting a synthetic chain, then runs the
full pipeline including JSON assembly. Verifies:
  - Payload schema is correct
  - All ticker payloads have the right shape
  - Walls/flip survive the round-trip
  - JSON serializes cleanly
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from ffgex_fetcher.__main__ import process_ticker
from ffgex_fetcher.output import build_payload


FIXTURE = Path(__file__).parent / "fixtures" / "spy_chain_synthetic.json"


@pytest.fixture
def chain():
    return json.loads(FIXTURE.read_text())


async def test_process_ticker_end_to_end(chain):
    """Run process_ticker on the synthetic SPY chain and validate output."""
    REF = datetime(2026, 5, 23, 14, 0, tzinfo=timezone.utc)
    result = await process_ticker(
        "SPY", chain, risk_free_rate=0.043, max_dte=30, now_utc=REF,
    )

    assert result["status"] == "ok"
    assert result["spot"] == 583.42
    assert result["futures_symbol"] == "ES"
    assert 9.5 < result["multiplier"] < 10.5
    assert result["call_wall"]["etf_strike"] == 590.0
    assert result["put_wall"]["etf_strike"] == 575.0
    assert result["gamma_flip"] is not None
    assert 575 < result["gamma_flip"]["etf_strike"] < 590
    assert result["contract_count"] > 100
    assert isinstance(result["expiries_included"], list)
    assert len(result["expiries_included"]) > 1
    # ES-mapped futures equiv should be ~5800
    assert 5700 < result["spot_futures_equiv"] < 5900


async def test_process_ticker_with_fetch_error():
    """If fetch_all_chains returned an exception for this ticker, output is
    an error record — the orchestrator must not crash."""
    err = TimeoutError("simulated CBOE timeout")
    result = await process_ticker("SPY", err, risk_free_rate=0.043, max_dte=30)
    assert result["status"] == "error"
    assert any("fetch failed" in w for w in result["warnings"])


async def test_process_ticker_with_unknown_ticker(chain):
    """A ticker not in DEFAULT_TICKERS should error gracefully."""
    result = await process_ticker("NOPE", chain, risk_free_rate=0.043, max_dte=30)
    assert result["status"] == "error"
    assert any("no configured" in w for w in result["warnings"])


async def test_full_payload_roundtrip(chain):
    """Build the full payload from a single-ticker pipeline and verify JSON
    serializes cleanly."""
    REF = datetime(2026, 5, 23, 14, 0, tzinfo=timezone.utc)
    spy = await process_ticker(
        "SPY", chain, risk_free_rate=0.043, max_dte=30, now_utc=REF,
    )
    payload = build_payload(
        {"SPY": spy}, risk_free_rate=0.043, fetch_run_id="20260523-1400",
        generated_at_utc=REF,
    )
    # Must serialize without error
    blob = json.dumps(payload)
    assert len(blob) > 1000
    # Round-trip
    back = json.loads(blob)
    assert back["schema_version"] == 2
    assert back["tickers"]["SPY"]["status"] == "ok"
    assert back["tickers"]["SPY"]["call_wall"]["etf_strike"] == 590.0
