"""
Output assembler — builds the JSON payload and posts it to the Cloudflare Worker.

Schema version 1. See docs/architecture.md for the full schema.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx


log = logging.getLogger(__name__)


SCHEMA_VERSION = 1
GENERATOR_NAME = "FFGEXFetcher 1.0"


def build_ticker_payload(
    *,
    status: str,
    spot: float | None,
    cfg,                         # TickerConfig
    multiplier: float,
    basis_carry: float,
    walls: dict,
    flip: float | None,
    contract_count: int,
    expiries_included: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    """Build the per-ticker section of the JSON output."""
    if status != "ok" or spot is None:
        return {"status": status, "warnings": warnings}

    def _map(strike: float) -> dict[str, float]:
        # Pre-computes both mapping methods for the indicator to choose.
        return {
            "etf_strike": float(strike),
            "futures_mult": float(strike * multiplier),
            "futures_basis": float(strike * cfg.scale_to_index + basis_carry),
        }

    out: dict[str, Any] = {
        "status": "ok",
        "spot": spot,
        "dividend_yield": cfg.dividend_yield,
        "futures_symbol": cfg.futures_symbol,
        "multiplier": multiplier,
        "spot_futures_equiv": spot * multiplier,
        "basis_carry": basis_carry,
        "gamma_flip": None,
        "call_wall": None,
        "put_wall": None,
        "top_pos_clusters": [],
        "top_neg_clusters": [],
        "top_oi_clusters": [],
        "total_net_gex": float(walls.get("total_net_gex", 0.0)),
        "contract_count": contract_count,
        "expiries_included": expiries_included,
        "warnings": warnings,
    }

    if flip is not None:
        out["gamma_flip"] = _map(flip)

    cw = walls.get("call_wall")
    if cw is not None:
        out["call_wall"] = {**_map(cw["strike"]), "gex_dollars": cw["gex_dollars"]}

    pw = walls.get("put_wall")
    if pw is not None:
        out["put_wall"] = {**_map(pw["strike"]), "gex_dollars": pw["gex_dollars"]}

    for c in walls.get("top_pos_clusters", []):
        out["top_pos_clusters"].append({**_map(c["strike"]), "gex_dollars": c["gex_dollars"]})
    for c in walls.get("top_neg_clusters", []):
        out["top_neg_clusters"].append({**_map(c["strike"]), "gex_dollars": c["gex_dollars"]})
    for c in walls.get("top_oi_clusters", []):
        out["top_oi_clusters"].append({**_map(c["strike"]), "open_interest": c["open_interest"]})

    return out


def build_payload(
    per_ticker_results: dict[str, dict],
    *,
    risk_free_rate: float,
    fetch_run_id: str,
    generated_at_utc: datetime | None = None,
) -> dict[str, Any]:
    """Build the top-level payload with all tickers and meta."""
    if generated_at_utc is None:
        generated_at_utc = datetime.now(timezone.utc)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at_utc.isoformat().replace("+00:00", "Z"),
        "generator": GENERATOR_NAME,
        "fetch_run_id": fetch_run_id,
        "macro": {
            "risk_free_rate": risk_free_rate,
            "source": "configured",
        },
        "tickers": per_ticker_results,
    }


async def post_to_worker(
    payload: dict[str, Any],
    *,
    worker_url: str,
    worker_secret: str,
    timeout_s: float = 30.0,
    retries: int = 3,
) -> None:
    """POST the payload to the Cloudflare Worker /update endpoint."""
    url = worker_url.rstrip("/") + "/update"
    headers = {"X-Auth": worker_secret, "Content-Type": "application/json"}
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code == 200:
                    log.info("Worker POST ok (%d bytes)", len(resp.content))
                    return
                if 500 <= resp.status_code < 600:
                    last_err = RuntimeError(f"Worker {resp.status_code}: {resp.text}")
                else:
                    raise RuntimeError(f"Worker {resp.status_code} (non-retryable): {resp.text}")
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            last_err = e
        if attempt < retries - 1:
            await asyncio.sleep(2 ** attempt)
    raise RuntimeError(f"Worker POST failed after {retries} attempts: {last_err}")
