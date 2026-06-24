"""
Unit tests for output.py (schema v2: blended + dte0 + expected_move).

Covers:
1. build_ticker_payload produces correct v2 schema for "ok" tickers.
2. Both blended and dte0 level sets are mapped correctly.
3. dte0 is None when there's no same-day expiry (dte0_contract_count=0).
4. expected_move_1d block.
5. MenthorQ-style 0DTE aliases.
6. Error variant.
7. build_payload envelope + JSON serializability.
8. post_to_worker — mocked httpx, auth/retry behavior.
"""
import json
from datetime import datetime, timezone
from unittest.mock import patch, AsyncMock

import pytest
import httpx

from ffgex_fetcher.output import (
    build_ticker_payload, build_payload, post_to_worker, SCHEMA_VERSION,
)
from ffgex_fetcher.futures_mapper import DEFAULT_TICKERS


def _walls(cw_strike=7500.0, pw_strike=7000.0):
    return {
        "call_wall": {"strike": cw_strike, "gex_dollars": 2.69e9},
        "put_wall": {"strike": pw_strike, "gex_dollars": -1.60e9},
        "top_pos_clusters": [
            {"strike": cw_strike + 10, "gex_dollars": 0.03e9},
            {"strike": cw_strike + 20, "gex_dollars": 0.02e9},
        ],
        "top_neg_clusters": [
            {"strike": pw_strike - 10, "gex_dollars": -0.02e9},
        ],
        "top_oi_clusters": [
            {"strike": cw_strike, "open_interest": 47305},
        ],
        "total_net_gex": 1.06e9,
    }


def _common_kwargs(**overrides):
    cfg = DEFAULT_TICKERS["SPX"]
    base = dict(
        status="ok", spot=7361.0, cfg=cfg, multiplier=1.0087, basis_carry=56.0,
        blended_walls=_walls(7500, 7000), blended_flip=7445.0,
        dte0_walls=_walls(7380, 7325), dte0_flip=7350.0, dte0_contract_count=210,
        expected_move=55.0, atm_iv=0.12,
        contract_count=2674, expiries_included=["2026-06-24", "2026-06-26"],
        warnings=[],
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Schema shape
# ---------------------------------------------------------------------------

def test_ticker_payload_v2_top_level_keys():
    out = build_ticker_payload(**_common_kwargs())
    expected = {
        "status", "spot", "dividend_yield", "futures_symbol", "underlying_kind",
        "multiplier", "spot_futures_equiv", "basis_carry",
        "blended", "dte0", "expected_move_1d",
        "contract_count", "dte0_contract_count", "expiries_included", "warnings",
    }
    assert set(out.keys()) == expected
    assert out["status"] == "ok"
    assert out["underlying_kind"] == "index"   # SPX scale_to_index == 1.0
    assert out["futures_symbol"] == "ES"


def test_blended_section_mapped():
    out = build_ticker_payload(**_common_kwargs())
    bl = out["blended"]
    assert bl["call_wall"]["etf_strike"] == 7500.0
    assert bl["put_wall"]["etf_strike"] == 7000.0
    assert bl["gamma_flip"]["etf_strike"] == 7445.0
    # multiplier mapping present
    assert bl["call_wall"]["futures_mult"] == pytest.approx(7500.0 * 1.0087)
    # blended carries OI clusters
    assert "top_oi_clusters" in bl
    assert len(bl["top_oi_clusters"]) == 1


def test_dte0_section_mapped_and_aliased():
    out = build_ticker_payload(**_common_kwargs())
    d0 = out["dte0"]
    assert d0 is not None
    assert d0["call_wall"]["etf_strike"] == 7380.0
    assert d0["put_wall"]["etf_strike"] == 7325.0
    assert d0["gamma_flip"]["etf_strike"] == 7350.0
    # MenthorQ-style aliases
    assert d0["call_resistance_0dte"]["etf_strike"] == 7380.0
    assert d0["put_support_0dte"]["etf_strike"] == 7325.0
    assert d0["hvl_0dte"]["etf_strike"] == 7350.0
    # gamma wall 0dte = larger |GEX| side; CW=2.69e9 > PW=1.60e9 → CW
    assert d0["gamma_wall_0dte"]["etf_strike"] == 7380.0
    # dte0 has NO oi clusters (we pass top_n_oi=0 upstream; output omits)
    assert "top_oi_clusters" not in d0


def test_dte0_none_when_no_same_day_expiry():
    out = build_ticker_payload(**_common_kwargs(dte0_contract_count=0))
    assert out["dte0"] is None


def test_expected_move_block():
    out = build_ticker_payload(**_common_kwargs())
    em = out["expected_move_1d"]
    assert em["move"] == 55.0
    assert em["atm_iv"] == 0.12
    assert em["high_etf"] == pytest.approx(7361.0 + 55.0)
    assert em["low_etf"] == pytest.approx(7361.0 - 55.0)
    assert em["high_futures_mult"] == pytest.approx((7361.0 + 55.0) * 1.0087)


def test_expected_move_none_when_unavailable():
    out = build_ticker_payload(**_common_kwargs(expected_move=None, atm_iv=None))
    assert out["expected_move_1d"] is None


def test_etf_underlying_kind():
    """A non-index config (scale != 1.0) reports underlying_kind=etf."""
    cfg = DEFAULT_TICKERS["IWM"]
    out = build_ticker_payload(**_common_kwargs(cfg=cfg, spot=298.0))
    assert out["underlying_kind"] == "etf"


def test_error_variant():
    out = build_ticker_payload(**_common_kwargs(status="error", spot=None,
                                                warnings=["fetch failed"]))
    assert out == {"status": "error", "warnings": ["fetch failed"]}


# ---------------------------------------------------------------------------
# build_payload envelope
# ---------------------------------------------------------------------------

def test_build_payload_envelope():
    ts = datetime(2026, 6, 24, 13, 40, tzinfo=timezone.utc)
    pay = build_payload(
        {"SPX": {"status": "ok"}, "NDX": {"status": "error", "warnings": []}},
        risk_free_rate=0.043, fetch_run_id="20260624-1340", generated_at_utc=ts,
    )
    assert pay["schema_version"] == SCHEMA_VERSION
    assert pay["schema_version"] == 2
    assert pay["generated_at"] == "2026-06-24T13:40:00Z"
    assert pay["fetch_run_id"] == "20260624-1340"
    assert pay["macro"]["risk_free_rate"] == 0.043
    assert "SPX" in pay["tickers"] and "NDX" in pay["tickers"]


def test_payload_json_serializable():
    out = build_ticker_payload(**_common_kwargs())
    pay = build_payload({"SPX": out}, risk_free_rate=0.043, fetch_run_id="x")
    blob = json.dumps(pay)
    assert len(blob) > 500
    back = json.loads(blob)
    assert back["tickers"]["SPX"]["dte0"]["call_wall"]["etf_strike"] == 7380.0


# ---------------------------------------------------------------------------
# post_to_worker — mocked
# ---------------------------------------------------------------------------

async def test_post_to_worker_success():
    payload = {"schema_version": 2, "tickers": {}}
    with patch("ffgex_fetcher.output.httpx.AsyncClient") as MockClient:
        mock = AsyncMock()
        mock.post = AsyncMock(return_value=httpx.Response(200, content=b'{"ok":true}'))
        MockClient.return_value.__aenter__.return_value = mock
        await post_to_worker(payload, worker_url="https://x.workers.dev", worker_secret="s")
        mock.post.assert_called_once()
        assert mock.post.call_args.args[0] == "https://x.workers.dev/update"
        assert mock.post.call_args.kwargs["headers"]["X-Auth"] == "s"


async def test_post_to_worker_retries_on_5xx():
    payload = {"schema_version": 2, "tickers": {}}
    with patch("ffgex_fetcher.output.httpx.AsyncClient") as MockClient:
        mock = AsyncMock()
        mock.post = AsyncMock(side_effect=[
            httpx.Response(503, content=b"x"),
            httpx.Response(503, content=b"x"),
            httpx.Response(200, content=b'{"ok":true}'),
        ])
        MockClient.return_value.__aenter__.return_value = mock
        with patch("ffgex_fetcher.output.asyncio.sleep", new=AsyncMock()):
            await post_to_worker(payload, worker_url="https://x.workers.dev",
                                 worker_secret="s", retries=3)
        assert mock.post.call_count == 3


async def test_post_to_worker_fails_after_retries():
    payload = {"schema_version": 2, "tickers": {}}
    with patch("ffgex_fetcher.output.httpx.AsyncClient") as MockClient:
        mock = AsyncMock()
        mock.post = AsyncMock(return_value=httpx.Response(503, content=b"x"))
        MockClient.return_value.__aenter__.return_value = mock
        with patch("ffgex_fetcher.output.asyncio.sleep", new=AsyncMock()):
            with pytest.raises(RuntimeError, match="failed after"):
                await post_to_worker(payload, worker_url="https://x.workers.dev",
                                     worker_secret="s", retries=2)


async def test_post_to_worker_no_retry_on_4xx():
    payload = {"schema_version": 2, "tickers": {}}
    with patch("ffgex_fetcher.output.httpx.AsyncClient") as MockClient:
        mock = AsyncMock()
        mock.post = AsyncMock(return_value=httpx.Response(401, content=b"unauth"))
        MockClient.return_value.__aenter__.return_value = mock
        with pytest.raises(RuntimeError, match="non-retryable"):
            await post_to_worker(payload, worker_url="https://x.workers.dev",
                                 worker_secret="bad")
        assert mock.post.call_count == 1
