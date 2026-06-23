/**
 * FF GEX Levels — Cloudflare Worker
 *
 * Endpoints:
 *   GET  /health              — public; returns service status and age
 *   POST /update              — auth via X-Auth; writes payload to KV
 *   GET  /gex                 — auth via X-Api-Key; full payload
 *   GET  /gex/{TICKER}        — auth via X-Api-Key; single ticker
 *
 * KV layout:
 *   full                       — last full payload JSON
 *   meta                       — small summary {generated_at, ticker_count, ...}
 *   ticker:{SYM}              — per-ticker section (with generated_at injected)
 *
 * Free-tier safety:
 *   - Writes ~24/day (well under 1k/day limit)
 *   - Reads ~300/day per chart (well under 100k/day limit)
 *   - Cache-Control max-age=60 lets the CF edge serve repeats for free
 */

const ALLOWED_TICKERS = new Set(["SPY", "QQQ", "IWM", "DIA", "GLD", "USO"]);

const json = (obj, init = {}) =>
  new Response(JSON.stringify(obj), {
    ...init,
    headers: {
      "content-type": "application/json",
      ...(init.headers || {}),
    },
  });

const cached = (body, maxAge = 60) =>
  new Response(body, {
    headers: {
      "content-type": "application/json",
      "cache-control": `public, max-age=${maxAge}`,
    },
  });

const unauthorized = () =>
  new Response("unauthorized", {
    status: 401,
    headers: { "content-type": "text/plain" },
  });

const badRequest = (msg) =>
  new Response(msg, {
    status: 400,
    headers: { "content-type": "text/plain" },
  });

const notFound = (msg = "not found") =>
  new Response(msg, {
    status: 404,
    headers: { "content-type": "text/plain" },
  });

const serverError = (msg) =>
  new Response(msg, {
    status: 500,
    headers: { "content-type": "text/plain" },
  });

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const path = url.pathname;
    const method = req.method;

    try {
      // ---- /health (public) ----
      if (method === "GET" && path === "/health") {
        const metaRaw = await env.GEX_KV.get("meta");
        if (!metaRaw) {
          return json({ status: "no_data" });
        }
        const meta = JSON.parse(metaRaw);
        const ageMin = Math.floor(
          (Date.now() - new Date(meta.generated_at).getTime()) / 60000
        );
        return json({
          status: "ok",
          last_update: meta.generated_at,
          age_minutes: ageMin,
          ticker_count: meta.ticker_count,
          schema_version: meta.schema_version,
          fetch_run_id: meta.fetch_run_id,
        });
      }

      // ---- POST /update (auth: WORKER_SECRET) ----
      if (method === "POST" && path === "/update") {
        if (req.headers.get("X-Auth") !== env.WORKER_SECRET) {
          return unauthorized();
        }
        const ct = req.headers.get("content-type") || "";
        if (!ct.includes("application/json")) {
          return badRequest("expected application/json");
        }
        let payload;
        try {
          payload = await req.json();
        } catch {
          return badRequest("invalid JSON");
        }

        if (
          !payload ||
          typeof payload !== "object" ||
          !payload.tickers ||
          typeof payload.tickers !== "object"
        ) {
          return badRequest("payload missing required fields (tickers)");
        }
        if (!payload.generated_at) {
          return badRequest("payload missing generated_at");
        }

        // Write full payload + meta in parallel.
        const writes = [];
        writes.push(env.GEX_KV.put("full", JSON.stringify(payload)));
        writes.push(
          env.GEX_KV.put(
            "meta",
            JSON.stringify({
              generated_at: payload.generated_at,
              schema_version: payload.schema_version || null,
              ticker_count: Object.keys(payload.tickers).length,
              fetch_run_id: payload.fetch_run_id || null,
            })
          )
        );

        // Per-ticker keys for cheap reads.
        for (const [sym, data] of Object.entries(payload.tickers)) {
          // Only persist allowed tickers; ignore the rest defensively.
          if (!ALLOWED_TICKERS.has(sym)) continue;
          writes.push(
            env.GEX_KV.put(
              `ticker:${sym}`,
              JSON.stringify({
                generated_at: payload.generated_at,
                schema_version: payload.schema_version,
                ticker: sym,
                ...data,
              })
            )
          );
        }
        await Promise.all(writes);

        return json({
          ok: true,
          tickers: Object.keys(payload.tickers),
          generated_at: payload.generated_at,
        });
      }

      // ---- GET /gex (full payload) ----
      if (method === "GET" && path === "/gex") {
        if (req.headers.get("X-Api-Key") !== env.READ_API_KEY) {
          return unauthorized();
        }
        const body = await env.GEX_KV.get("full");
        if (!body) return notFound("no payload yet");
        return cached(body, 60);
      }

      // ---- GET /gex/{TICKER} ----
      if (method === "GET" && path.startsWith("/gex/")) {
        if (req.headers.get("X-Api-Key") !== env.READ_API_KEY) {
          return unauthorized();
        }
        const parts = path.split("/").filter(Boolean); // ["gex", "SPY"]
        if (parts.length !== 2) return badRequest("bad path");
        const sym = parts[1].toUpperCase();
        if (!ALLOWED_TICKERS.has(sym)) return notFound("unknown ticker");
        const body = await env.GEX_KV.get(`ticker:${sym}`);
        if (!body) return notFound(`no data for ${sym}`);
        return cached(body, 60);
      }

      // ---- Root: brief identification ----
      if (method === "GET" && path === "/") {
        return new Response("FF GEX Levels — service is up\n", {
          headers: { "content-type": "text/plain" },
        });
      }

      return notFound();
    } catch (err) {
      return serverError(`internal error: ${err.message || err}`);
    }
  },
};
