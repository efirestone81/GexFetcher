# Installation Guide

This walks through setting up FF GEX Levels from scratch. Total time: ~30 minutes one-time service setup, ~2 minutes per NinjaTrader PC.

---

## Part 1 — Cloud service (one-time, ~30 min)

### Prereqs
- A GitHub account (free)
- A Cloudflare account (free)
- Node.js 22+ installed locally (for wrangler CLI)

### 1.1 Create the repo on GitHub
1. Create a new **public** repository (public gives unlimited GitHub Actions minutes; we don't put any secrets in code)
2. Clone this codebase into it and push.

### 1.2 Deploy the Cloudflare Worker

```sh
cd worker
npm install -g wrangler
wrangler login                  # opens browser to authenticate

# Create a KV namespace and copy the returned id into wrangler.toml
wrangler kv namespace create GEX_KV
#   ⇒  { binding = "GEX_KV", id = "abc123..." }
# Edit wrangler.toml, replace REPLACE_WITH_KV_NAMESPACE_ID

# Set the two secrets (use long random strings; e.g. `openssl rand -hex 32`)
wrangler secret put WORKER_SECRET    # for GitHub Actions → Worker
wrangler secret put READ_API_KEY     # for NinjaTrader → Worker

# Deploy
wrangler deploy
#   ⇒  Published https://ff-gex.YOUR_SUBDOMAIN.workers.dev
```

Save the URL — that's your `WORKER_URL`.

### 1.3 Verify with curl

```sh
# Should return {"status":"no_data"} since we haven't posted anything yet
curl https://ff-gex.YOUR_SUBDOMAIN.workers.dev/health
```

### 1.4 Configure GitHub Actions secrets

In your repo: **Settings → Secrets and variables → Actions → New repository secret**

Add:
- `WORKER_URL` = your worker URL (e.g. `https://ff-gex.YOUR_SUBDOMAIN.workers.dev`)
- `WORKER_SECRET` = the same value you set as the Worker secret above

### 1.5 First manual run

In the repo: **Actions → Compute GEX → Run workflow → Run**

Wait ~60 seconds. Then:

```sh
curl https://ff-gex.YOUR_SUBDOMAIN.workers.dev/health
# Should return {"status":"ok","age_minutes":0,...}
```

If you see this, the cloud service is fully operational. Cron will fire at the scheduled times (UTC: 07:00, 13:35, 16:00, 19:55 weekdays).

---

## Part 2 — NinjaTrader (each PC, ~2 min)

### 2.1 Copy the indicator file

Copy `indicator/FFGEXLevels.cs` to:
```
%USERPROFILE%\Documents\NinjaTrader 8\bin\Custom\Indicators\FFGEXLevels.cs
```

### 2.2 Compile

1. Open NinjaTrader 8
2. Tools → NinjaScript Editor
3. Press **F5** (compile)
4. The Output window should show: `Compile complete -- 0 errors`

### 2.3 Add to chart

1. Open a chart for ES, NQ, RTY, YM, GC, or CL
2. Right-click → Indicators
3. Find **FF GEX Levels** under Indicators
4. Add it, then configure:
   - **Ticker:** `Auto` (will detect ES→SPY, NQ→QQQ, etc.)
   - **Service URL:** your Worker URL
   - **API Key:** the `READ_API_KEY` value from Part 1.2
   - Leave everything else at defaults

Click OK. Within 5 seconds you should see horizontal lines on the chart and a status banner in the top-left.

### 2.4 Verify

- The status banner should read `FF GEX [SPY] Xm ago` in gray
- You should see at least the Call Wall (red), Put Wall (green), and Gamma Flip (gold dashed)
- Numbers in the banner should match what `/health` returns

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Banner says "connecting…" forever | URL or API key wrong | Check indicator settings; test with curl |
| Banner says "STALE Xh" | No fresh data from cron | Check GitHub Actions → Compute GEX log |
| No lines appear | Walls are off-chart | Zoom out price scale, or set narrower strike range |
| Indicator fails to compile | NT8 version too old | NT8 must be 8.0.28+ |
| "Auto" mode picks wrong ticker | Custom instrument name | Set Ticker explicitly to SPY/QQQ/etc. |

## Where things live

| What | Where |
|---|---|
| Indicator log output | NinjaTrader: View → NinjaScript Output |
| Local cache | `%USERPROFILE%\Documents\NinjaTrader 8\FFGEX\cache_TICKER.json` |
| GHA run logs | github.com/YOU/ff-gex-service/actions |
| Worker logs | dash.cloudflare.com → Workers → ff-gex → Logs |
| Manual fetch trigger | GHA → Compute GEX → Run workflow |

## Updating

When you update the indicator file:
1. Replace the `.cs` file in the Custom\Indicators folder
2. F5 in NinjaScript Editor to recompile
3. Remove the indicator from the chart, re-add it (NT8 doesn't hot-reload running indicators)
