/**
 * Worker unit tests.
 *
 * These exercise the routing, auth, and payload validation logic without
 * needing the full Wrangler / workerd runtime. We feed Request objects into
 * `worker.fetch()` and verify Response.
 *
 * Run with:  node --test test/worker.test.js
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import worker from "../src/index.js";

// --- Fake KV that mirrors what Cloudflare KV provides at runtime. -------
class FakeKV {
  constructor() { this.store = new Map(); }
  async get(k) { return this.store.has(k) ? this.store.get(k) : null; }
  async put(k, v) { this.store.set(k, v); }
  async delete(k) { this.store.delete(k); }
}

function mkEnv(overrides = {}) {
  return {
    GEX_KV: new FakeKV(),
    WORKER_SECRET: "write-secret",
    READ_API_KEY: "read-key",
    ...overrides,
  };
}

const sampleSpyPayload = {
  schema_version: 1,
  generated_at: new Date().toISOString(),
  generator: "FFGEXFetcher 1.0",
  fetch_run_id: "20260523-1400",
  macro: { risk_free_rate: 0.043, source: "configured" },
  tickers: {
    SPY: {
      status: "ok",
      spot: 583.42,
      futures_symbol: "ES",
      multiplier: 9.94,
      call_wall: { etf_strike: 590, futures_mult: 5865, gex_dollars: 2.7e9 },
      put_wall: { etf_strike: 575, futures_mult: 5716, gex_dollars: -1.6e9 },
      gamma_flip: { etf_strike: 582.15, futures_mult: 5787, futures_basis: 5836 },
      total_net_gex: 1.06e9,
    },
    QQQ: { status: "error", warnings: ["fetch failed"] },
  },
};

// ===========================================================================
// /health
// ===========================================================================

test("health returns no_data on empty KV", async () => {
  const env = mkEnv();
  const resp = await worker.fetch(new Request("https://x/health"), env);
  assert.equal(resp.status, 200);
  const body = await resp.json();
  assert.equal(body.status, "no_data");
});

test("health returns age after update", async () => {
  const env = mkEnv();
  await worker.fetch(
    new Request("https://x/update", {
      method: "POST",
      headers: { "X-Auth": "write-secret", "content-type": "application/json" },
      body: JSON.stringify(sampleSpyPayload),
    }),
    env
  );
  const resp = await worker.fetch(new Request("https://x/health"), env);
  const body = await resp.json();
  assert.equal(body.status, "ok");
  assert.equal(typeof body.age_minutes, "number");
  assert.ok(body.age_minutes >= 0 && body.age_minutes < 60);
  assert.equal(body.ticker_count, 2);
});

// ===========================================================================
// /update
// ===========================================================================

test("update rejects bad auth", async () => {
  const env = mkEnv();
  const resp = await worker.fetch(
    new Request("https://x/update", {
      method: "POST",
      headers: { "X-Auth": "WRONG", "content-type": "application/json" },
      body: JSON.stringify(sampleSpyPayload),
    }),
    env
  );
  assert.equal(resp.status, 401);
});

test("update rejects non-json content type", async () => {
  const env = mkEnv();
  const resp = await worker.fetch(
    new Request("https://x/update", {
      method: "POST",
      headers: { "X-Auth": "write-secret", "content-type": "text/plain" },
      body: "not json",
    }),
    env
  );
  assert.equal(resp.status, 400);
});

test("update rejects payload without tickers", async () => {
  const env = mkEnv();
  const resp = await worker.fetch(
    new Request("https://x/update", {
      method: "POST",
      headers: { "X-Auth": "write-secret", "content-type": "application/json" },
      body: JSON.stringify({ schema_version: 1, generated_at: new Date().toISOString() }),
    }),
    env
  );
  assert.equal(resp.status, 400);
});

test("update writes full + meta + per-ticker keys", async () => {
  const env = mkEnv();
  const resp = await worker.fetch(
    new Request("https://x/update", {
      method: "POST",
      headers: { "X-Auth": "write-secret", "content-type": "application/json" },
      body: JSON.stringify(sampleSpyPayload),
    }),
    env
  );
  assert.equal(resp.status, 200);
  const body = await resp.json();
  assert.equal(body.ok, true);
  assert.deepEqual(body.tickers.sort(), ["QQQ", "SPY"]);

  // KV should now have: full, meta, ticker:SPY, ticker:QQQ
  assert.ok(await env.GEX_KV.get("full"));
  assert.ok(await env.GEX_KV.get("meta"));
  assert.ok(await env.GEX_KV.get("ticker:SPY"));
  assert.ok(await env.GEX_KV.get("ticker:QQQ"));
});

test("update ignores unknown ticker symbols", async () => {
  const env = mkEnv();
  const evil = {
    ...sampleSpyPayload,
    tickers: { SPY: sampleSpyPayload.tickers.SPY, HACKER: { status: "ok" } },
  };
  await worker.fetch(
    new Request("https://x/update", {
      method: "POST",
      headers: { "X-Auth": "write-secret", "content-type": "application/json" },
      body: JSON.stringify(evil),
    }),
    env
  );
  assert.ok(await env.GEX_KV.get("ticker:SPY"));
  // HACKER must not be persisted as a per-ticker key
  assert.equal(await env.GEX_KV.get("ticker:HACKER"), null);
});

// ===========================================================================
// /gex
// ===========================================================================

test("GET /gex requires API key", async () => {
  const env = mkEnv();
  await worker.fetch(
    new Request("https://x/update", {
      method: "POST",
      headers: { "X-Auth": "write-secret", "content-type": "application/json" },
      body: JSON.stringify(sampleSpyPayload),
    }),
    env
  );

  // No key
  let r = await worker.fetch(new Request("https://x/gex"), env);
  assert.equal(r.status, 401);
  // Wrong key
  r = await worker.fetch(
    new Request("https://x/gex", { headers: { "X-Api-Key": "wrong" } }),
    env
  );
  assert.equal(r.status, 401);
  // Right key
  r = await worker.fetch(
    new Request("https://x/gex", { headers: { "X-Api-Key": "read-key" } }),
    env
  );
  assert.equal(r.status, 200);
  const body = await r.json();
  assert.equal(body.schema_version, 1);
});

test("GET /gex returns 404 before first update", async () => {
  const env = mkEnv();
  const r = await worker.fetch(
    new Request("https://x/gex", { headers: { "X-Api-Key": "read-key" } }),
    env
  );
  assert.equal(r.status, 404);
});

test("GET /gex includes cache-control header", async () => {
  const env = mkEnv();
  await worker.fetch(
    new Request("https://x/update", {
      method: "POST",
      headers: { "X-Auth": "write-secret", "content-type": "application/json" },
      body: JSON.stringify(sampleSpyPayload),
    }),
    env
  );
  const r = await worker.fetch(
    new Request("https://x/gex", { headers: { "X-Api-Key": "read-key" } }),
    env
  );
  assert.match(r.headers.get("cache-control"), /max-age=\d+/);
});

// ===========================================================================
// /gex/{ticker}
// ===========================================================================

test("GET /gex/SPY returns single-ticker payload", async () => {
  const env = mkEnv();
  await worker.fetch(
    new Request("https://x/update", {
      method: "POST",
      headers: { "X-Auth": "write-secret", "content-type": "application/json" },
      body: JSON.stringify(sampleSpyPayload),
    }),
    env
  );
  const r = await worker.fetch(
    new Request("https://x/gex/SPY", { headers: { "X-Api-Key": "read-key" } }),
    env
  );
  assert.equal(r.status, 200);
  const body = await r.json();
  assert.equal(body.ticker, "SPY");
  assert.equal(body.spot, 583.42);
  assert.equal(body.call_wall.etf_strike, 590);
  // generated_at injected into per-ticker payload
  assert.ok(body.generated_at);
});

test("GET /gex/spy is case-insensitive", async () => {
  const env = mkEnv();
  await worker.fetch(
    new Request("https://x/update", {
      method: "POST",
      headers: { "X-Auth": "write-secret", "content-type": "application/json" },
      body: JSON.stringify(sampleSpyPayload),
    }),
    env
  );
  const r = await worker.fetch(
    new Request("https://x/gex/spy", { headers: { "X-Api-Key": "read-key" } }),
    env
  );
  assert.equal(r.status, 200);
});

test("GET /gex/XYZ returns 404 for unknown ticker", async () => {
  const env = mkEnv();
  await worker.fetch(
    new Request("https://x/update", {
      method: "POST",
      headers: { "X-Auth": "write-secret", "content-type": "application/json" },
      body: JSON.stringify(sampleSpyPayload),
    }),
    env
  );
  const r = await worker.fetch(
    new Request("https://x/gex/XYZ", { headers: { "X-Api-Key": "read-key" } }),
    env
  );
  assert.equal(r.status, 404);
});

// ===========================================================================
// misc
// ===========================================================================

test("GET / returns service identification", async () => {
  const env = mkEnv();
  const r = await worker.fetch(new Request("https://x/"), env);
  assert.equal(r.status, 200);
  const text = await r.text();
  assert.match(text, /FF GEX/);
});

test("GET /unknown returns 404", async () => {
  const env = mkEnv();
  const r = await worker.fetch(new Request("https://x/random"), env);
  assert.equal(r.status, 404);
});

test("end-to-end: update then read recovers exact payload", async () => {
  const env = mkEnv();
  await worker.fetch(
    new Request("https://x/update", {
      method: "POST",
      headers: { "X-Auth": "write-secret", "content-type": "application/json" },
      body: JSON.stringify(sampleSpyPayload),
    }),
    env
  );
  const r = await worker.fetch(
    new Request("https://x/gex", { headers: { "X-Api-Key": "read-key" } }),
    env
  );
  const body = await r.json();
  assert.equal(body.tickers.SPY.call_wall.etf_strike, 590);
  assert.equal(body.tickers.SPY.gamma_flip.etf_strike, 582.15);
  assert.equal(body.macro.risk_free_rate, 0.043);
});
