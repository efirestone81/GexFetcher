"""
Unit tests for output.py.

Covers:
1. build_ticker_payload produces correct schema for "ok" tickers.
2. build_ticker_payload produces error variant for failed tickers.
3. build_payload top-level wrapper.
4. post_to_worker — mock httpx and verify headers/URL/retry behavior.
"""

import json
from datetime import datetime, timezone
from unittest.mock import patch, AsyncMock

import pytest
import httpx

from ffgex_fetcher.output import build_ticker_payload, build_payload, post_to_worker, SCHEMA_VERSION
from ffgex_fetcher.futures_mapper import DEFAULT_TICKERS


# ---------------------------------------------------------------------------
# build_ticker_payload
# ---------------------------------------------------------------------------

def _sample_walls():
    return {
        "call_wall": {"strike": 590.0, "gex_dollars": 2.69e9},
        "put_wall": {"strike": 575.0, "gex_dollars": -1.60e9},
        "top_pos_clusters": [
            {"strike": 584.0, "gex_dollars": 0.03e9},
            {"strike": 582.0, "gex_dollars": 0.03e9},
        ],
        "top_neg_clusters": [
            {"strike": 585.0, "gex_dollars": -0.02e9},
        ],
        "top_oi_clusters": [
            {"strike": 590.0, "open_interest": 473057},
            {"strike": 575.0, "open_interest": 435619},
        ],
        "total_net_gex": 1.06e9,
    }


def test_ticker_payload_ok_full_schema():
    cfg = DEFAULT_TICKERS["SPY"]
    out = build_ticker_payload(
        status="ok",
        spot=583.42,
        cfg=cfg,
        multiplier=9.9414,
        basis_carry=14.79,
        walls=_sample_walls(),
        flip=582.15,
        contract_count=714,
        expiries_included=["2026-05-23", "2026-05-30"],
        warnings=[],
    )

    # Required top-level keys
    expected_keys = {
        "status", "spot", "dividend_yield", "futures_symbol", "multiplier",
        "spot_futures_equiv", "basis_carry", "gamma_flip", "call_wall",
        "put_wall", "top_pos_clusters", "top_neg_clusters", "top_oi_clusters",
        "total_net_gex", "contract_count", "expiries_included", "warnings",
    }
    assert set(out.keys()) == expected_keys
    assert out["status"] == "ok"
    assert out["spot"] == 583.42
    assert out["futures_symbol"] == "ES"
    assert out["multiplier"] == 9.9414

    # Flip carries all three mapping fields
    assert set(out["gamma_flip"].keys()) == {"etf_strike", "futures_mult", "futures_basis"}
    assert out["gamma_flip"]["etf_strike"] == 582.15
    assert out["gamma_flip"]["futures_mult"] == pytest.approx(582.15 * 9.9414)
    assert out["gamma_flip"]["futures_basis"] == pytest.approx(582.15 * 10.0 + 14.79)

    # Wall payloads carry mapping + dollars
    assert set(out["call_wall"].keys()) == {"etf_strike", "futures_mult", "futures_basis", "gex_dollars"}
    assert out["call_wall"]["etf_strike"] == 590.0
    assert out["call_wall"]["gex_dollars"] == 2.69e9

    # Clusters preserve count
    assert len(out["top_pos_clusters"]) == 2
    assert len(out["top_neg_clusters"]) == 1
    assert len(out["top_oi_clusters"]) == 2


def test_ticker_payload_error_minimal():
    out = build_ticker_payload(
        status="error",
        spot=None,
        cfg=DEFAULT_TICKERS["SPY"],
        multiplier=1.0,
        basis_carry=0.0,
        walls={},
        flip=None,
        contract_count=0,
        expiries_included=[],
        warnings=["fetch failed: timeout"],
    )
    assert out == {"status": "error", "warnings": ["fetch failed: timeout"]}


def test_ticker_payload_missing_walls():
    cfg = DEFAULT_TICKERS["SPY"]
    walls_empty = {
        "call_wall": None,
        "put_wall": None,
        "top_pos_clusters": [],
        "top_neg_clusters": [],
        "top_oi_clusters": [],
        "total_net_gex": 0.0,
    }
    out = build_ticker_payload(
        status="ok",
        spot=100.0,
        cfg=cfg,
        multiplier=1.0,
        basis_carry=0.0,
        walls=walls_empty,
        flip=None,
        contract_count=0,
        expiries_included=[],
        warnings=[],
    )
    assert out["call_wall"] is None
    assert out["put_wall"] is None
    assert out["gamma_flip"] is None
    assert out["top_pos_clusters"] == []


# ---------------------------------------------------------------------------
# build_payload
# ---------------------------------------------------------------------------

def test_build_payload_envelope():
    ts = datetime(2026, 5, 23, 14, 0, 0, tzinfo=timezone.utc)
    pay = build_payload(
        per_ticker_results={
            "SPY": {"status": "ok", "spot": 583.42},
            "QQQ": {"status": "error", "warnings": ["..."]},
        },
        risk_free_rate=0.043,
        fetch_run_id="20260523-1400",
        generated_at_utc=ts,
    )
    assert pay["schema_version"] == SCHEMA_VERSION
    assert pay["generated_at"] == "2026-05-23T14:00:00Z"
    assert pay["generator"].startswith("FFGEXFetcher")
    assert pay["fetch_run_id"] == "20260523-1400"
    assert pay["macro"]["risk_free_rate"] == 0.043
    assert "SPY" in pay["tickers"]
    assert "QQQ" in pay["tickers"]


def test_build_payload_serializable():
    """The full payload must be JSON-serializable — no numpy types leaking."""
    pay = build_payload(
        per_ticker_results={"SPY": {"status": "ok", "spot": 583.42}},
        risk_free_rate=0.043,
        fetch_run_id="20260523-1400",
    )
    s = json.dumps(pay)
    assert len(s) > 100


# ---------------------------------------------------------------------------
# post_to_worker — mocked
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_post_to_worker_success():
    """Mock AsyncClient.post → 200 → success."""
    payload = {"schema_version": 1, "tickers": {}}
    with patch("ffgex_fetcher.output.httpx.AsyncClient") as MockClient:
        mock = AsyncMock()
        mock.post = AsyncMock(return_value=httpx.Response(200, content=b'{"ok":true}'))
        MockClient.return_value.__aenter__.return_value = mock

        await post_to_worker(
            payload, worker_url="https://gex.workers.dev", worker_secret="hunter2",
        )
        mock.post.assert_called_once()
        called_url = mock.post.call_args.args[0]
        called_headers = mock.post.call_args.kwargs["headers"]
        assert called_url == "https://gex.workers.dev/update"
        assert called_headers["X-Auth"] == "hunter2"


@pytest.mark.asyncio
async def test_post_to_worker_retries_on_5xx():
    """5xx should trigger retry, then succeed if a later attempt returns 200."""
    payload = {"schema_version": 1, "tickers": {}}
    with patch("ffgex_fetcher.output.httpx.AsyncClient") as MockClient:
        mock = AsyncMock()
        # First two calls return 503, third returns 200.
        mock.post = AsyncMock(side_effect=[
            httpx.Response(503, content=b"unavailable"),
            httpx.Response(503, content=b"unavailable"),
            httpx.Response(200, content=b'{"ok":true}'),
        ])
        MockClient.return_value.__aenter__.return_value = mock

        # Use no sleep delay for fast test
        with patch("ffgex_fetcher.output.asyncio.sleep", new=AsyncMock()):
            await post_to_worker(
                payload, worker_url="https://gex.workers.dev",
                worker_secret="hunter2", retries=3,
            )
        assert mock.post.call_count == 3


@pytest.mark.asyncio
async def test_post_to_worker_fails_after_retries():
    payload = {"schema_version": 1, "tickers": {}}
    with patch("ffgex_fetcher.output.httpx.AsyncClient") as MockClient:
        mock = AsyncMock()
        mock.post = AsyncMock(return_value=httpx.Response(503, content=b"unavail"))
        MockClient.return_value.__aenter__.return_value = mock

        with patch("ffgex_fetcher.output.asyncio.sleep", new=AsyncMock()):
            with pytest.raises(RuntimeError, match="failed after"):
                await post_to_worker(
                    payload, worker_url="https://gex.workers.dev",
                    worker_secret="x", retries=2,
                )


@pytest.mark.asyncio
async def test_post_to_worker_no_retry_on_4xx():
    payload = {"schema_version": 1, "tickers": {}}
    with patch("ffgex_fetcher.output.httpx.AsyncClient") as MockClient:
        mock = AsyncMock()
        mock.post = AsyncMock(return_value=httpx.Response(401, content=b"unauthorized"))
        MockClient.return_value.__aenter__.return_value = mock

        with pytest.raises(RuntimeError, match="non-retryable"):
            await post_to_worker(
                payload, worker_url="https://gex.workers.dev",
                worker_secret="bad-secret",
            )
        # Should have been called exactly once (no retry).
        assert mock.post.call_count == 1
