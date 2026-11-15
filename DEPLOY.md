# Deploying Tradewinds to Fly.io

Everything is configured in `fly.toml`, `Dockerfile` and `docker-entrypoint.sh`. One machine, one process:
rate limits and the run queue are in memory, and today's spend is persisted on the `/data` volume.

## First deploy

```bash
fly auth login
fly apps create tradewinds            # or pick another name and change `app` in fly.toml
fly volumes create tradewinds_data --size 1 --region iad
fly secrets set ANTHROPIC_API_KEY=sk-ant-...
# optional tracing
fly secrets set LANGFUSE_PUBLIC_KEY=pk-lf-... LANGFUSE_SECRET_KEY=sk-lf-... LANGFUSE_BASE_URL=https://cloud.langfuse.com
fly deploy
fly scale count 1                     # keep exactly one machine
```

Then open `https://<app>.fly.dev` and run one search. `fly logs` shows each request; with Langfuse keys set, each run
appears as a `plan-options` / `plan-trip` trace grouped by the browser's session id.

## Automatic deploys

`.github/workflows/ci.yml` runs pytest, the offline integration test and a Docker build on every push and PR.
Pushes to `main` also deploy once you add a `FLY_API_TOKEN` repository secret (`fly tokens create deploy`).

## Guardrails (all in `fly.toml` `[env]` or as secrets)

| Setting | Default | What it protects |
|---|---|---|
| `TRADEWINDS_DAILY_BUDGET_USD` | 20 | Hard daily cap on estimated API spend (persisted on the volume) |
| `TRADEWINDS_RUN_BUDGET_USD` | 1.00 | One search or plan can't cost more than this |
| `TRADEWINDS_OPTIONS_PER_HOUR` / `TRADEWINDS_PLANS_PER_HOUR` | 20 / 10 | Per visitor (Fly-Client-IP, IPv6 grouped by /64) |
| `TRADEWINDS_MAX_CONCURRENT` / `TRADEWINDS_MAX_QUEUE` | 2 / 6 | Model calls in flight and runs waiting |
| `TRADEWINDS_TRUST_PROXY` | 1 on Fly | Only Fly's own client-IP header is trusted |
| `TRADEWINDS_RUNS_KEEP` | 500 | Run logs kept on the volume |

Also set a monthly spend limit on the Anthropic key in the Anthropic Console as a last line of defence.

## Before real traffic

- **Map tiles:** the app uses tile.openstreetmap.org, whose usage policy doesn't allow heavy traffic. Switch to a
  tile provider (MapTiler, Stadia, Thunderforest) in `frontend/app.js` (`TILES`) and the CSP `img-src` in `backend/app.py`.
- **Privacy:** traces and run logs contain what travelers type. The footer says so; add a privacy page if you collect more.

## Deploys and running trips

A deploy restarts the one machine. uvicorn gets 20 seconds (`--timeout-graceful-shutdown`) to let open streams finish,
within Fly's 60-second `kill_timeout`; anything still running after that is dropped, and the traveler sees "Try again".
Deploy at quiet times. Spend already made is on the volume's ledger, so caps survive the restart.

Each visitor (IP, IPv6 /64) can spend at most `TRADEWINDS_IP_DAILY_BUDGET_USD` (default $3, about 20 searches with plans)
per UTC day, so one person can't use up the site-wide `TRADEWINDS_DAILY_BUDGET_USD`.
