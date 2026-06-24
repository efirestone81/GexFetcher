"""Output assembler — builds JSON payload and posts to the Cloudflare Worker."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2   # bumped: added dte0 block + expected_move_1d
GENERATOR_NAME = "FFGEXFetcher 1.1"


def _map_level(strike, multiplier, basis_carry, cfg):
    return {
        "etf_strike": float(strike),
        "futures_mult": float(strike * multiplier),
        "futures_basis": float(strike * cfg.scale_to_index + basis_carry),
    }


def _wall_block(wall, multiplier, basis_carry, cfg):
    if wall is None:
        return None
    return {**_map_level(wall["strike"], multiplier, basis_carry, cfg),
            "gex_dollars": wall["gex_dollars"]}


def _cluster_list(clusters, multiplier, basis_carry, cfg, key="gex_dollars"):
    out = []
    for c in clusters:
        entry = _map_level(c["strike"], multiplier, basis_carry, cfg)
        if key == "open_interest":
            entry["open_interest"] = c["open_interest"]
        else:
            entry["gex_dollars"] = c["gex_dollars"]
        out.append(entry)
    return out


def build_levels_section(walls, flip, multiplier, basis_carry, cfg, *,
                         include_oi=True):
    """Build a levels block (used for both blended and 0DTE sets)."""
    section: dict[str, Any] = {
        "gamma_flip": _map_level(flip, multiplier, basis_carry, cfg) if flip is not None else None,
        "call_wall": _wall_block(walls.get("call_wall"), multiplier, basis_carry, cfg),
        "put_wall": _wall_block(walls.get("put_wall"), multiplier, basis_carry, cfg),
        "top_pos_clusters": _cluster_list(walls.get("top_pos_clusters", []), multiplier, basis_carry, cfg),
        "top_neg_clusters": _cluster_list(walls.get("top_neg_clusters", []), multiplier, basis_carry, cfg),
        "total_net_gex": float(walls.get("total_net_gex", 0.0)),
    }
    if include_oi:
        section["top_oi_clusters"] = _cluster_list(
            walls.get("top_oi_clusters", []), multiplier, basis_carry, cfg, key="open_interest")
    return section


def build_ticker_payload(*, status, spot, cfg, multiplier, basis_carry,
                         blended_walls, blended_flip,
                         dte0_walls, dte0_flip, dte0_contract_count,
                         expected_move, atm_iv,
                         contract_count, expiries_included, warnings):
    if status != "ok" or spot is None:
        return {"status": status, "warnings": warnings}

    out: dict[str, Any] = {
        "status": "ok",
        "spot": spot,
        "dividend_yield": cfg.dividend_yield,
        "futures_symbol": cfg.futures_symbol,
        "underlying_kind": "index" if cfg.scale_to_index == 1.0 else "etf",
        "multiplier": multiplier,
        "spot_futures_equiv": spot * multiplier,
        "basis_carry": basis_carry,
        # Blended (all expiries <= max_dte) — structural levels
        "blended": build_levels_section(blended_walls, blended_flip, multiplier, basis_carry, cfg, include_oi=True),
        # 0DTE (today's expiry only) — intraday levels. None if no same-day expiry.
        "dte0": None,
        "expected_move_1d": None,
        "contract_count": contract_count,
        "dte0_contract_count": dte0_contract_count,
        "expiries_included": expiries_included,
        "warnings": warnings,
    }

    if dte0_contract_count > 0:
        out["dte0"] = build_levels_section(dte0_walls, dte0_flip, multiplier, basis_carry, cfg, include_oi=False)
        # MenthorQ-style aliases for the 0DTE primary levels
        out["dte0"]["call_resistance_0dte"] = out["dte0"]["call_wall"]
        out["dte0"]["put_support_0dte"] = out["dte0"]["put_wall"]
        out["dte0"]["hvl_0dte"] = out["dte0"]["gamma_flip"]
        # Gamma Wall 0DTE = the single strike with the largest |GEX| (today only)
        gw = _gamma_wall(dte0_walls)
        out["dte0"]["gamma_wall_0dte"] = (
            _wall_block(gw, multiplier, basis_carry, cfg) if gw else None)

    if expected_move is not None and spot is not None:
        out["expected_move_1d"] = {
            "move": expected_move,
            "atm_iv": atm_iv,
            "high_etf": spot + expected_move,
            "low_etf": spot - expected_move,
            "high_futures_mult": (spot + expected_move) * multiplier,
            "low_futures_mult": (spot - expected_move) * multiplier,
        }

    return out


def _gamma_wall(walls):
    """The strike with the largest absolute GEX (call or put side)."""
    cw = walls.get("call_wall")
    pw = walls.get("put_wall")
    candidates = []
    if cw:
        candidates.append((abs(cw["gex_dollars"]), cw))
    if pw:
        candidates.append((abs(pw["gex_dollars"]), pw))
    if not candidates:
        return None
    return max(candidates, key=lambda t: t[0])[1]


def build_payload(per_ticker_results, *, risk_free_rate, fetch_run_id,
                  generated_at_utc=None):
    if generated_at_utc is None:
        generated_at_utc = datetime.now(timezone.utc)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at_utc.isoformat().replace("+00:00", "Z"),
        "generator": GENERATOR_NAME,
        "fetch_run_id": fetch_run_id,
        "macro": {"risk_free_rate": risk_free_rate, "source": "configured"},
        "tickers": per_ticker_results,
    }


async def post_to_worker(payload, *, worker_url, worker_secret,
                         timeout_s=30.0, retries=3):
    url = worker_url.rstrip("/") + "/update"
    headers = {"X-Auth": worker_secret, "Content-Type": "application/json"}
    last_err = None
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
