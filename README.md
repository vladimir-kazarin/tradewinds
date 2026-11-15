# Tradewinds 🌴

**A trip planner for backpackers. Tell it where you'd like to go, answer a couple of questions, and a crew of seven AI agents finds three best-value trips and plans the one you pick.**

> "Somewhere warm and cheap in December, I'm burnt out. Want to learn to surf, great food, no party hostels."

Every agent hunts for value: cheap gateways and overland legs, hostel dorms or guesthouses, street food, free and cheap activities, and never over your budget. The Brief agent asks only what's missing (dates, origin, budget, dorm or private, must-sees, one country or several). Then you get three trips, each with a rough total and a per-day cost. Open one and the rest of the crew plans it: a day-by-day itinerary on a map, flights and stays that match the itinerary's towns, a budget, a reviewer sign-off, and prefilled flight and stay searches. While they work you see a spinning globe with the crew in a ring around it and one friendly status line.

## Run it

```bash
pip install -r requirements-dev.txt         # app deps + pytest
export ANTHROPIC_API_KEY=sk-ant-...         # leave unset to replay the recorded demo
cd backend && uvicorn app:app --reload --port 8000
# open http://localhost:8000   (add ?demo=1 to force the demo with a key set)
```

Tests and evals:

```bash
pytest                                   # unit + API tests (offline)
python demo/make_demo.py                 # integration test of both steps; rewrites demo/*_run.jsonl
python evals/run_eval.py --limit 3       # LIVE eval smoke (~$0.60 per case with the plan step)
python evals/run_eval.py --judge --label sonnet55          # full 20-case set + Haiku value judge
python evals/run_eval.py --compare evals/results/A.json evals/results/B.json
```

All settings are listed in `.env.example`: the model, spend caps (per run, per day), per-IP hourly limits, the queue cap, `TRADEWINDS_TRUST_PROXY` for Fly.io, and optional Langfuse keys for tracing.

## How it feels

Like chatting with a friend who has backpacked everywhere. It asks one or two questions at a time, and as soon as it knows the basics (roughly when, from where, budget) it starts searching while you keep chatting: the trip summary appears in seconds, three candidate trips about 25 seconds later, ranked for value about 10 seconds after that. Pick one and the day-by-day plan streams in piece by piece (itinerary, then flights and beds, then costs), reviewed in about 50 seconds. Anything you add in the chat meanwhile ("dorms are fine", "I'd love a cooking class") is folded into the plan.

## Architecture

```
 STEP 1 · POST /api/options                               STEP 2 · POST /api/plan (on click)
 chat ─► Brief ─┬─► brief ─► Scout ─► candidates ─► Judge ─► 3 options    brief + option + notes ─► Itinerary ─► Logistics ─► Budget ─► Reviewer
   (Haiku)      │            (Sonnet)               (Haiku)                                         (Sonnet)     (Sonnet)     (Haiku)   (Sonnet)
                └─► 1-2 questions (up to 2 rounds)                                      each part streams to the UI as it finishes; one revision max
```

The browser carries the brief and the chosen option between the two calls, so the server keeps no session.

| Piece | File | What it does |
|---|---|---|
| Agent runtime | `backend/agent.py` | Tool-use loop on the raw Anthropic SDK (no framework). Tools run in parallel, tool errors go back to the model, and every agent must finish through a `submit` tool (`tool_choice: auto` with a nudge, since current models reject forced tool choice). |
| Contracts | `backend/schemas.py` | Each agent finishes by calling a `submit` tool whose schema is a Pydantic model. Sums and date arithmetic are validators, so bad output goes back to the model with the error. |
| Agents | `backend/crew.py` | The seven agents as data. The Brief's second round uses an output with no room for questions, which is what enforces one question round. |
| Orchestration | `backend/pipeline.py` | `plan_options()` and `plan_trip()`, plain async. Logistics runs after the itinerary so stays match its towns; a reviewer rejection re-runs both. |
| Checks | `backend/checks.py` | Code checks across agents: stays vs itinerary bases and nights, countries vs the chosen trip. Mismatches go back to the agent once. |
| Booking links | `backend/links.py` | Google Flights and Booking.com searches built from validated fields. The model never writes URLs. |
| Status line | `backend/status.py` | Turns agent events into friendly sentences ("Checking December weather near Nosara…"). No tokens spent. |
| Tools | `backend/tools.py` | Geocoding, historical climate for the travel dates, live wave outlook (Open-Meteo, no API key). |
| MCP server | `backend/mcp_server.py` | The same tools over MCP: `claude mcp add tradewinds -- python backend/mcp_server.py` |
| Streaming | `backend/app.py` | FastAPI + SSE over POST. Live runs are recorded as JSONL; without a key the endpoints replay `demo/*_run.jsonl`. A semaphore caps concurrent runs and a disconnect cancels the run. |
| Limits | `backend/limits.py` | Per-IP hourly rate limits, a queue cap, a per-run cost cap and a daily spend cap, so a public key can't be drained. |
| Tracing | `backend/tracing.py` | Langfuse: a trace per run, grouped by the traveler's session, with an agent observation per helper, a generation per model call (usage and cost) and a tool observation per tool call. Off without keys. |
| Evals | `evals/` | 20 backpacker prompts with expected facts, deterministic checks, an optional Claude Haiku 5.5 value judge, and run-to-run comparison. |
| UI | `frontend/` | Vanilla JS modules, no build: landing, chat + trip canvas, crew ring, ticket-style cards, progressive plan with map, booking links, costs and saving tips. |
| Deploy | `Dockerfile`, `fly.toml`, `DEPLOY.md` | One Fly machine with a `/data` volume, security headers, CI that tests, builds and deploys from `main`. |

## Design decisions

- **Why no framework?** The loop is about 80 lines, and owning it makes retries, validation, cost tracking and termination easy to see and control.
- **Why a fixed pipeline and not an LLM router?** The order of steps is known in advance, so code handles control flow and the model handles judgment. Runs are cheaper, more predictable and easier to evaluate.
- **Why options first, plan on click?** Planning three trips fully would triple the cost and the wait. A rough cost per option is enough to choose; the full plan replaces it.
- **Why code for links, checks and status?** Anything that has to be exactly right (URLs, sums, matching towns) is computed or checked in code rather than trusted to the model.
- **Why two model tiers?** Brief, Judge and Budget are well-specified (extraction, scoring, arithmetic), so Haiku 5.5 does them fast and cheaply; finding places, planning days, pricing logistics and reviewing stay on Sonnet 5.5. Measured on the eval: about $0.14 per search + plan, down from $0.60.
- **Next steps:** deploy (see `DEPLOY.md`), a map tile provider for real traffic, real flight prices instead of model estimates (the eval judge still flags optimistic fares), prompt caching, and saved/shareable trips.
