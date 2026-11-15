# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Tradewinds is a multi-agent trip planner for backpackers, built as a consumer product: every agent looks for best-value options (cheap but never grim, never over budget). A traveler chats with a Brief agent (at most one round of follow-up questions), gets three trip options with a rough cost, and opens one to have the rest of the crew plan it. Seven Claude agents in a fixed pipeline do the work, and progress streams to a single-page UI over SSE. It is written directly on the raw Anthropic SDK with no agent framework. That is a deliberate choice (see README "Design decisions"), so don't introduce LangChain/LangGraph or similar. The UI must stay non-technical: never show tool names, thoughts, scores, token counts or reviewer notes to travelers.

Specs: `SPEC.md` (V1 baseline) and `SPEC-product-flow.md` (current product flow delta).

## Commands

```bash
pip install -r requirements-dev.txt        # requirements.txt + pytest

# Run the server (must run from backend/, since modules use flat imports like `from tools import ...`)
cd backend && uvicorn app:app --reload --port 8000     # http://localhost:8000  (?demo=1 forces replay)

# Unit + API tests (tests/conftest.py puts backend/ on sys.path)
pytest
pytest tests/test_links.py::test_open_jaw_flight_url   # single test

# Integration test of both steps + regenerate the demo recordings (no API key, no network)
python demo/make_demo.py

# LIVE eval (spends money; ~$0.60/case with the plan step). Results in evals/results/
python evals/run_eval.py --limit 3
python evals/run_eval.py --judge --label <name>
python evals/run_eval.py --compare evals/results/A.json evals/results/B.json

# Expose the tools over MCP
claude mcp add tradewinds -- python backend/mcp_server.py
```

`demo/make_demo.py` runs the real pipeline with a scripted fake model and stubbed tools through three runs (vague prompt → questions; answer → 3 options; plan the best option) and asserts on the result (one question round, 3 ranked options with consistent rough costs and one multi-country option, reviewer approved after one revision, logistics re-ran after the revision, stays match itinerary bases, sums add up, links are https). **It overwrites `demo/questions_run.jsonl`, `demo/options_run.jsonl` and `demo/plan_run.jsonl`**, which the server replays in demo mode. Commit them with any event or schema change.

Env vars: see `.env.example`. `ANTHROPIC_API_KEY` (if unset, both endpoints replay the demo), `TRADEWINDS_MODEL`, spend/abuse limits (`TRADEWINDS_RUN_BUDGET_USD`, `TRADEWINDS_DAILY_BUDGET_USD`, `TRADEWINDS_OPTIONS_PER_HOUR`, `TRADEWINDS_PLANS_PER_HOUR`, `TRADEWINDS_MAX_CONCURRENT`, `TRADEWINDS_MAX_QUEUE`, `TRADEWINDS_TRUST_PROXY`), and `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_BASE_URL` (tracing is a no-op without them). The server doesn't read `.env` itself: export it (`set -a; . ./.env; set +a`).

## Architecture

```
STEP 1  POST /api/options   chat → Brief ─┬─ 1-2 questions (up to 2 rounds)
                                          └─ brief (+ optional followups) → Scout → candidates → Judge → 3 ranked options
STEP 2  POST /api/plan      brief + option + notes → Itinerary → Logistics → Budget → Reviewer
                                                        ↑  plan_part after each      │
                                                        └─ revise, MAX_REVISIONS=1 ──┘
```
Brief, Judge and Budget run on `TRADEWINDS_LIGHT_MODEL` (Haiku 5.5); Scout, Itinerary, Logistics and Reviewer on `TRADEWINDS_MODEL` (Sonnet 5.5). Each agent sets an `effort` level, the main latency lever.

- **`backend/agent.py`**: the whole runtime. `Agent.run()` loops over model calls with `tool_choice: auto` (Claude Sonnet 5.5 and later reject forced `any`/`tool` with a 400): `SUBMIT_RULE` is appended to every system prompt, a text-only reply gets a nudge to call `submit`, and the last turn is announced. Every agent finishes by calling a synthetic `submit` tool whose `input_schema` is `output.model_json_schema()`. If Pydantic validation fails, the error goes back to the model as an `is_error` tool_result. Real tool calls in a turn run concurrently. Every `tool_use` must get a `tool_result`, including skipped ones.
- **`backend/schemas.py`**: Pydantic models that serve as the contracts between agents, plus single-model invariants as validators (brief nights = date span, rough cost and budget totals = sum of parts, IATA airport codes, `BriefResult` has questions xor brief). Changing a field changes the submit schema the model sees, the JSON passed downstream, the recordings, and what the frontend renders.
- **`backend/crew.py`**: agent definitions only. `CREW` is the 7 agents in UI ring order; `ALL_AGENTS` adds `brief_decide_agent` (same name `brief`, output `BriefDecision`), used when the transcript already has an assistant turn: this is what makes a second question round impossible.
- **`backend/pipeline.py`**: `plan_options(turns, emit)` and `plan_trip(brief, option, emit)`, plain async. Agents receive upstream outputs as JSON text (`_j()`). `rank_options()` orders by the judge's ranking/pick with fallbacks to scout order. Itinerary runs before Logistics; `checks.py` mismatches trigger one re-run of the responsible agent; a reviewer rejection re-runs Itinerary and then Logistics. All emits go through `status.with_status`.
- **`backend/checks.py`**: code checks returning issue sentences: itinerary day count and dates vs the brief, day and stay countries vs the option, stays vs itinerary base runs. Names compare casefolded and accent-stripped.
- **`backend/links.py`**: Google Flights / Booking.com search URLs from validated fields. Stay links are built from the itinerary's own base runs (`pipeline.final_links`), so they can't name a town the itinerary doesn't sleep in. The model never writes URLs.
- **`backend/status.py`**: wraps `emit`, adding friendly `status` events from agent/tool events.
- **`backend/limits.py`**: `RateLimiter` (per-IP sliding window; app keys IPv6 by /64 and trusts only `Fly-Client-IP`), `Slots` (running+queued cap, reserved synchronously in `app._admit` so bursts can't overshoot), `SpendLedger` (daily UTC spend, persisted to `TRADEWINDS_LEDGER_PATH`), `cost_usd()` price table. `agent.Usage` charges every model call (and an estimate for a cancelled one) to the ledger and raises `RunBudgetExceeded` / `DailyBudgetExhausted`. Option and plan totals are also checked against the budget in code (`checks.option_budget_issues`, `plan_budget_issues`).
- **`backend/tracing.py`**: Langfuse (SDK v5) via `start_as_current_observation`: root span per run (`plan-options` / `plan-trip`, session = browser search id), `agent` per helper, `generation` per model call, `tool` per tool call. Every helper is a no-op without keys.
- **`evals/`**: `prompts.jsonl` (20 cases with `expect`), `scoring.py` (pure checks, unit tested), `run_eval.py` (live runner, Haiku 5.5 judge, `--compare`).
- **`backend/tools.py`**: Open-Meteo tools (geocode, historical climate proxy, live marine). `TOOL_REGISTRY` maps name → (async fn, Anthropic tool schema). `run_tool()` never raises; errors come back as `{"error": ...}` dicts so the model can recover. `mcp_server.py` wraps the same `run_tool` (mcp>=2 `MCPServer` API).
- **`backend/app.py`**: FastAPI + `sse-starlette`. Live requests pass `_admit` first (daily cap → 503, queue cap / per-IP limit → 429 with a traveler-facing `detail`; demo replays are never limited). POST endpoints validated by `OptionsRequest` / `PlanRequest` (≤8 turns, last from user; plan bodies ≤30k chars, nights 1–60, finite in-range coordinates). 422 responses never echo the input. Live runs log every event to `runs/<ms>.jsonl` with `_delay` (opened only after the semaphore is acquired). Demo replay picks `questions_run` vs `options_run` by whether the transcript has an assistant turn; planning a non-recorded option prepends a "Demo: showing a sample plan" status. `usage` events are log-only. Exceptions reach the browser as a generic friendly `error`.
- **`frontend/`**: no build step. `index.html` (landing + app shell), `app.css` (indie backpacker look), `render.js` (pure HTML builders; every model string through `esc()`, links through `safeUrl()`), `app.js` (state replaced via `setState`, SSE over `fetch` since EventSource can't POST, chat/plan flows, notes, map). The app view is chat left + trip canvas right (tabs under 900px). Vendored Leaflet and fonts; tiles from tile.openstreetmap.org (also allowed in the CSP).
- **Deploy**: `Dockerfile` + `docker-entrypoint.sh` (fixes `/data` volume ownership, runs as uid 10001), `fly.toml` (one machine, `/data` volume for ledger and run logs), `.github/workflows/ci.yml` (tests, offline integration test, docker build, deploy from main with `FLY_API_TOKEN`). See `DEPLOY.md`.

### Event protocol

`run_start {step, agents}`, `agent_start`, `agent_done`, `agent_skip` (step 2 marks Brief/Scout/Judge done), `status {text}`, `questions {reply, questions}` (terminal), `brief {reply, brief, followups}` (search started; the UI shows the summary and keeps the chat open, later messages become plan `notes`), `candidates {options}` (Scout's 3, before ranking), `options {reply, brief, options:[{option, pitch, best}]}` (terminal), `plan_part {part: itinerary|logistics|budget, ...}` (re-sent after a revision; logistics carries `links`), `plan {option_name, itinerary, logistics, budget, review, links}` (terminal), `revision`, `agent_error`, `error` (terminal), and `usage` (log only, never streamed). `thought`, `tool_call` and `tool_result` are emitted and recorded but the UI ignores them. Backend, `frontend/app.js` handlers and the recordings must stay in sync; regenerate the demo after any change.

### Demo script coupling

`demo/make_demo.py` monkeypatches `agent._client` and `agent.run_tool`. `FakeMessages` identifies the calling agent by **matching the `system` prompt (plus `agent.SUBMIT_RULE`) against `crew.ALL_AGENTS`**, then pops the next scripted response from that agent's list in `SCRIPTS` (both Brief variants share the `brief` list). Fake tool results are keyed by `(tool, place)` or `(tool, lat)`. As a result:
- Changing a system prompt is fine, but adding an agent, adding a turn, a check-triggered re-run, or changing the revision flow requires matching entries in `SCRIPTS`. Otherwise the script hits `StopIteration` or a missing key.
- Schema changes require updating the scripted submit payloads (`QUESTIONS`, `DECISION`, `OPTS`, `VERDICT`, `ITIN`, `LOG`, `BUDGET`, …), or validation fails in a loop until max_turns.
