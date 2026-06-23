"""
Cross-stack integration: Python fetcher POSTs to a real local HTTP server.

This proves the Python → Worker contract works:
  - URL is constructed correctly
  - Auth header is sent
  - JSON body is valid
  - Server-side validation expectations are met

The server here mirrors what the Cloudflare Worker does for /update —
both pieces must agree on the contract.
"""

from __future__ import annotations

import asyncio
import http.server
import json
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ffgex_fetcher.__main__ import process_ticker
from ffgex_fetcher.output import build_payload, post_to_worker


FIXTURE = Path(__file__).parent / "fixtures" / "spy_chain_synthetic.json"


class FakeWorkerHandler(http.server.BaseHTTPRequestHandler):
    """Mirrors the validation logic of worker/src/index.js POST /update."""
    WORKER_SECRET = "test-secret-123"
    received_payloads: list[dict] = []

    def log_message(self, *args, **kwargs):
        pass  # silence

    def do_POST(self):
        if self.path != "/update":
            self.send_response(404)
            self.end_headers()
            return
        if self.headers.get("X-Auth") != self.WORKER_SECRET:
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"unauthorized")
            return
        ct = self.headers.get("content-type", "")
        if "application/json" not in ct:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"expected json")
            return
        length = int(self.headers.get("content-length", "0"))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except Exception:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"bad json")
            return
        # Worker requires these fields
        if not isinstance(body, dict) or "tickers" not in body or "generated_at" not in body:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"missing fields")
            return
        self.__class__.received_payloads.append(body)
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({
            "ok": True,
            "tickers": list(body["tickers"].keys()),
        }).encode())


def _find_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def fake_worker():
    """Spin up a local HTTP server that imitates the Cloudflare Worker."""
    FakeWorkerHandler.received_payloads = []
    port = _find_port()
    server = http.server.HTTPServer(("127.0.0.1", port), FakeWorkerHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", FakeWorkerHandler
    finally:
        server.shutdown()
        thread.join(timeout=2)


async def test_python_to_worker_full_path(fake_worker):
    """Build a real payload (from synthetic chain) and POST it; verify the
    server received exactly what we expect."""
    url, handler = fake_worker
    chain = json.loads(FIXTURE.read_text())
    REF = datetime(2026, 5, 23, 14, 0, tzinfo=timezone.utc)
    spy = await process_ticker("SPY", chain, 0.043, 30, now_utc=REF)
    payload = build_payload(
        {"SPY": spy}, risk_free_rate=0.043, fetch_run_id="IT-1",
        generated_at_utc=REF,
    )

    await post_to_worker(
        payload, worker_url=url, worker_secret=handler.WORKER_SECRET,
    )

    assert len(handler.received_payloads) == 1
    got = handler.received_payloads[0]
    assert got["schema_version"] == 1
    assert got["fetch_run_id"] == "IT-1"
    assert "SPY" in got["tickers"]
    assert got["tickers"]["SPY"]["call_wall"]["etf_strike"] == 590.0
    assert got["tickers"]["SPY"]["spot"] == 583.42


async def test_python_to_worker_bad_secret_fails(fake_worker):
    """If we send the wrong secret, the Worker returns 401 and post_to_worker
    must raise."""
    url, handler = fake_worker
    payload = build_payload(
        {"SPY": {"status": "ok"}}, risk_free_rate=0.043, fetch_run_id="bad",
    )
    with pytest.raises(RuntimeError):
        await post_to_worker(
            payload, worker_url=url, worker_secret="WRONG-SECRET",
            retries=1,
        )
    assert handler.received_payloads == []


async def test_python_to_worker_validates_payload_shape(fake_worker):
    """The server checks for `tickers` and `generated_at`; missing → 400."""
    url, handler = fake_worker
    bad_payload = {"schema_version": 1}  # missing tickers + generated_at
    with pytest.raises(RuntimeError):
        await post_to_worker(
            bad_payload, worker_url=url, worker_secret=handler.WORKER_SECRET,
            retries=1,
        )
