# Tradewinds 🌴

**Tell it where you'd like to go, answer a couple of questions, and a crew of seven AI agents finds three trips and plans the one you pick.**

> "Somewhere warm in December, I'm burnt out. Want to learn to surf, great food, no party scene."

The Brief agent asks only what's missing (dates, origin, budget, must-sees, one country or several). Then you get three trips, each with a rough total cost. Open one and the rest of the crew plans it: a day-by-day itinerary on a map, flights and stays that match the itinerary's towns, a budget, a reviewer sign-off, and prefilled flight and stay searches. While they work you see a spinning globe with the crew in a ring around it and one friendly status line.

## Run it

```bash
pip install -r requirements-dev.txt         # app deps + pytest
export ANTHROPIC_API_KEY=sk-ant-...         # leave unset to replay the recorded demo
cd backend && uvicorn app:app --reload --port 8000
# open http://localhost:8000   (add ?demo=1 to force the demo with a key set)
```

Tests:

```bash
pytest                       # unit + API tests
python demo/make_demo.py     # integration test of both steps; rewrites demo/*_run.jsonl
```

Optional settings: `TRADEWINDS_MODEL` (default `claude-sonnet-5-5`) and `TRADEWINDS_MAX_CONCURRENT` (default 2 live runs).

## Architecture

```
 STEP 1 · POST /api/options                         STEP 2 · POST /api/plan (per card, on click)
 chat ─► Brief ─┬─► Scout ─► Judge ─► 3 options     brief + option ─► Itinerary ─► Logistics
                │   geocode,  rank,   rough cost                       geocode,     stays per base
                │   climate   pitch                                    ocean           │
                └─► questions (one round max)                                          ▼
                                                                   Budget ─► Reviewer ─┐
                                                                      ▲   revise ≤ 2   │
                                                                      └────────────────┘
```

The browser carries the brief and the chosen option between the two calls, so the server keeps no session.

| Piece | File | What it does |
|---|---|---|
| Agent runtime | `backend/agent.py` | Tool-use loop on the raw Anthropic SDK (no framework). Tools run in parallel, tool errors go back to the model, and the last turn forces `submit`. |
| Contracts | `backend/schemas.py` | Each agent finishes by calling a `submit` tool whose schema is a Pydantic model. Sums and date arithmetic are validators, so bad output goes back to the model with the error. |
| Agents | `backend/crew.py` | The seven agents as data. The Brief's second round uses an output with no room for questions, which is what enforces one question round. |
| Orchestration | `backend/pipeline.py` | `plan_options()` and `plan_trip()`, plain async. Logistics runs after the itinerary so stays match its towns; a reviewer rejection re-runs both. |
| Checks | `backend/checks.py` | Code checks across agents: stays vs itinerary bases and nights, countries vs the chosen trip. Mismatches go back to the agent once. |
| Booking links | `backend/links.py` | Google Flights and Booking.com searches built from validated fields. The model never writes URLs. |
| Status line | `backend/status.py` | Turns agent events into friendly sentences ("Checking December weather near Nosara…"). No tokens spent. |
| Tools | `backend/tools.py` | Geocoding, historical climate for the travel dates, live wave outlook (Open-Meteo, no API key). |
| MCP server | `backend/mcp_server.py` | The same tools over MCP: `claude mcp add tradewinds -- python backend/mcp_server.py` |
| Streaming | `backend/app.py` | FastAPI + SSE over POST. Live runs are recorded as JSONL; without a key the endpoints replay `demo/*_run.jsonl`. A semaphore caps concurrent runs and a disconnect cancels the run. |
| UI | `frontend/index.html` | One page, vanilla JS: ask, follow-up chat, globe with crew ring, three cards, planned trip with map, budget and booking links. |

## Design decisions

- **Why no framework?** The loop is about 80 lines, and owning it makes retries, validation, cost tracking and termination easy to see and control.
- **Why a fixed pipeline and not an LLM router?** The order of steps is known in advance, so code handles control flow and the model handles judgment. Runs are cheaper, more predictable and easier to evaluate.
- **Why options first, plan on click?** Planning three trips fully would triple the cost and the wait. A rough cost per option is enough to choose; the full plan replaces it.
- **Why code for links, checks and status?** Anything that has to be exactly right (URLs, sums, matching towns) is computed or checked in code rather than trusted to the model.
- **Next steps:** deploy to Fly.io with spend caps and rate limiting, an eval set of about 20 prompts, tracing, prompt caching, and saved/shareable trips.
