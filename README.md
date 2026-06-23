# FF GEX Levels

**FlowForged Gamma Exposure horizontal levels for NinjaTrader 8 — sourced from CBOE, distributed via Cloudflare, rendered on ES/NQ/RTY/YM/GC/CL futures charts.**

Always-on cloud architecture: GitHub Actions runs the GEX computation 4x per trading day, posts to a Cloudflare Worker, and NinjaTrader pulls the latest levels on a 5-minute polling interval. Levels are computed even when your PC is offline.

## Architecture

```
GitHub Actions (cron 4x/day weekdays)
  └─► Python fetcher (CBOE CDN → BS gamma → walls/flip → JSON)
       └─► POST https://ff-gex.YOUR.workers.dev/update  (X-Auth)
            └─► Cloudflare KV store
                 └─► GET /gex/{SPY|QQQ|...}  (X-Api-Key)
                      └─► NinjaScript indicator on ES/NQ charts
```

## Components

| Path | What |
|---|---|
| `fetcher/` | Python 3.12 service. Modular: greeks, engine, futures mapper, output. 119 tests. |
| `worker/` | Cloudflare Worker (JS). Routes, auth, KV. 16 tests. |
| `indicator/FFGEXLevels.cs` | NinjaScript indicator. Weighted horizontal lines, status banner, local cache fallback. 31 parsing tests. |
| `.github/workflows/` | Cron (`compute.yml`), CI (`ci.yml`), keepalive (`keepalive.yml`). |

## Quick start

### One-time service setup
1. Fork this repo to GitHub (or create your own with this code, public for unlimited GHA minutes)
2. Create Cloudflare account → install wrangler → deploy worker:
   ```sh
   cd worker
   npx wrangler kv namespace create GEX_KV
   # Paste the returned id into wrangler.toml
   npx wrangler secret put WORKER_SECRET   # generate a random string
   npx wrangler secret put READ_API_KEY    # generate another random string
   npx wrangler deploy
   ```
3. In GitHub repo settings → Secrets and variables → Actions, add:
   - `WORKER_URL` — your `https://ff-gex.YOURSUBDOMAIN.workers.dev`
   - `WORKER_SECRET` — same value as above
4. Trigger workflow manually (Actions tab → Compute GEX → Run workflow) to verify
5. Confirm: `curl -H "X-Api-Key: YOUR_READ_KEY" https://ff-gex.YOURSUBDOMAIN.workers.dev/gex/SPY`

### NinjaTrader setup
1. Copy `indicator/FFGEXLevels.cs` → `%USERPROFILE%\Documents\NinjaTrader 8\bin\Custom\Indicators\`
2. In NinjaTrader → New → NinjaScript Editor → press F5 to compile
3. Add the "FF GEX Levels" indicator to an ES (or NQ/RTY/YM/GC/CL) chart
4. In the indicator settings, paste your `ServiceUrl` and `ApiKey`

## Math foundation

GEX per contract:
```
GEX_i = Γ_i × OI_i × 100 × S² × 0.01 × sign_i
        where sign = +1 for calls, −1 for puts (SqueezeMetrics convention)
```

Black-Scholes gamma:
```
d1 = [ln(S/K) + (r − q + σ²/2)·T] / (σ·√T)
Γ  = e^(−qT) · φ(d1) / (S · σ · √T)
```

Gamma flip: nearest zero-crossing of total net GEX swept across spot in
±10% range at 0.05% step, with linear interpolation between adjacent
sweep points.

Futures mapping: two methods are pre-computed and stored in JSON; the
indicator picks at display time:
- **Dynamic Multiplier**: `futures_price = etf_strike × (futures_ref / etf_spot)`
- **Cost-of-Carry**: `futures_price = etf_strike × scale + etf_spot × (e^((r−q)·T) − 1) × scale`

## Refresh schedule (UTC)

| Time | Trigger | Rationale |
|---|---|---|
| 07:00 (~02:00 ET) | Daily | Overnight OCC OI final |
| 13:35 weekdays | RTH | Post-open levels (DST: 09:35 ET) |
| 16:00 weekdays | RTH | Midday refresh (DST: 12:00 ET) |
| 19:55 weekdays | RTH | Pre-close snapshot (DST: 15:55 ET) |

## Tickers covered

| ETF | Futures | Scale |
|---|---|---|
| SPY | ES (S&P 500 E-mini) | ~10× |
| QQQ | NQ (Nasdaq-100 E-mini) | ~40× |
| IWM | RTY (Russell 2000 E-mini) | ~10× |
| DIA | YM (Dow E-mini) | ~100× |
| GLD | GC (Gold) | ~12× |
| USO | CL (Crude Oil) | ~1× |

## Cost

All free. GitHub Actions free tier (unlimited minutes on public repos), Cloudflare Workers free tier (100k req/day), CBOE public CDN (free, undocumented endpoint).

## Test coverage

- **Python**: 119 tests across greeks (24), engine (36), mapper (11), audit (14), pipeline (2), output (9), cross-stack (3), orchestrator (4), 14 stage-4 audits, 2 live (skipped without network)
- **Worker**: 16 routing/auth/KV tests via Node's built-in test runner
- **Indicator parsing**: 31 C# tests validating the parsing logic against real Worker payloads

## License

[your choice]
