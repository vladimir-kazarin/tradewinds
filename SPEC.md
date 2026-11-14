# Spec: Tradewinds — V1 (as-built baseline)

Status: proposed
Scope: describes the system **as it exists today**. Later changes (the demo stage, then the product stage) get their own delta specs (`SPEC-<change>.md`) measured against this one.

## Objective

Tradewinds turns a free-form, emotional travel prompt into a complete trip plan using a fixed pipeline of seven Claude agents. Every agent step and tool call streams live to a single-page UI.

> "Two weeks from Dec 1, burnt out, want chill + tropical, learn to surf, great food, no party scene. From Boston, ~$3k."

**Users, staged:**

| Stage | User | What success looks like |
|---|---|---|
| **1. Local (this spec)** | The developer, on their own machine | One command starts it. A live run with a key produces a reviewed plan. A run without a key replays the demo. The integration test passes offline. |
| 2. Demo (deferred) | Interviewers or portfolio visitors on a public URL | Anyone can watch the demo. Live runs are gated and capped on cost. |
| 3. Product (deferred) | Real travelers | Accounts, persistence, evals, observability, abuse protection. |

**User stories (V1):**
- As a user, I type a prompt and watch seven agents work, including their thoughts, tool calls and tool results, in real time.
- As a user, I get a scored shortlist of 2–4 destinations, a chosen destination, a day-by-day itinerary on a map, flights and stays, a budget, and a reviewer verdict.
- As a user with no API key, I can still see a full run, replayed from a recording at its original timing.
- As the developer, I can use the same weather and geo tools from Claude Code over MCP.

## Tech Stack

- **Python 3.12** (any 3.10+ should work given `from __future__ import annotations` and `X | Y` types)
- `anthropic>=0.40`: raw SDK, `AsyncAnthropic(max_retries=3)`. **No agent framework.**
- `fastapi>=0.110`, `uvicorn>=0.29`, `sse-starlette>=2.0`
- `pydantic>=2.7`: agent contracts and submit-tool schemas
- `httpx>=0.27`: Open-Meteo calls (geocoding, archive, marine; no API key)
- `mcp>=2.0`: `MCPServer` API (stdio)
- Frontend: one `index.html`, vanilla JS, vendored Leaflet and fonts, **no build step**
- Model: `TRADEWINDS_MODEL`, default `claude-sonnet-5-5`

## Commands

```bash
# Install
pip install -r requirements.txt

# Run (must be started from backend/, because modules use flat imports)
cd backend && uvicorn app:app --reload --port 8000        # http://localhost:8000

# Live mode needs a key. Without one, /api/plan always replays the demo.
export ANTHROPIC_API_KEY=sk-ant-...

# Integration test (no key, no network). WARNING: overwrites demo/sample_run.jsonl
python demo/make_demo.py

# Health check
curl -s localhost:8000/api/health        # {"ok": true, "live": <bool>}

# Replay the demo stream directly
curl -N "localhost:8000/api/plan?demo=1&speed=4"

# MCP
claude mcp add tradewinds -- python backend/mcp_server.py
```

Env vars: `ANTHROPIC_API_KEY`, `TRADEWINDS_MODEL` (default `claude-sonnet-5-5`), `TRADEWINDS_MAX_CONCURRENT` (default `2`).

## Architecture

```
prompt ─► Brief ─► Scout ─► Vibe Judge ─► ┬─ Itinerary ─┬─► Budget ─► Reviewer ─► final
                    │ geocode,             └─ Logistics ─┘                │
                    │ climate_for_window      (parallel)                  │ rejected?
                    ▼                                                     ▼
              Open-Meteo                       Itinerary revises, then Budget + Reviewer re-run
                                               (MAX_REVISIONS = 2, i.e. up to 3 review rounds)
```

### Agents (`backend/crew.py`)

| name | role (UI label) | output model | tools | max_turns | max_tokens |
|---|---|---|---|---|---|
| `brief` | Trip Brief | `TripBrief` | none | 2 | 4096 |
| `scout` | Destination Scout | `ScoutReport` (2–4 options) | `geocode`, `climate_for_window` | 8 | 4096 |
| `judge` | Vibe Judge | `VibeVerdict` | none | 2 | 4096 |
| `itinerary` | Itinerary Builder | `Itinerary` | `geocode`, `ocean_conditions` | 8 | 8000 |
| `logistics` | Logistics | `Logistics` | none | 2 | 4096 |
| `budget` | Budget | `Budget` | none | 2 | 4096 |
| `reviewer` | Reviewer | `Review` | none | 2 | 4096 |

Orchestration rules:
- Upstream outputs are passed **as JSON text in the user message** (`_j()`), never as shared mutable state.
- If the judge's `pick` doesn't match a scout option name (case-insensitive), the pipeline falls back to `options[0]`. The other options become `alternatives`.
- `itinerary` and `logistics` run concurrently via `asyncio.gather`, and both receive only the brief plus the chosen destination.
- In the revision loop, only `itinerary` is re-run, with the reviewer's issues. `logistics` is never revised.

### Agent runtime contract (`backend/agent.py`)

1. Each turn calls `messages.create` with `tool_choice: {"type": "any"}`. On the **last** turn it forces `{"type": "tool", "name": "submit"}`.
2. Every agent finishes by calling a synthetic `submit` tool whose `input_schema` is `output.model_json_schema()`.
3. A valid submit returns immediately. If submit fails Pydantic validation, the error goes back as an `is_error` `tool_result`.
4. Real tool calls in a turn run concurrently. **Every `tool_use` gets a `tool_result`**, and any not run are answered with `"skipped"`.
5. A tool result containing an `"error"` key is sent with `is_error: true`.
6. API errors (`APIStatusError`, `APIConnectionError`) emit `agent_error` and re-raise. Running out of turns raises `RuntimeError`.
7. Token usage is accumulated across all agents into one `Usage` and reported on `final`.

### Tools (`backend/tools.py`)

| tool | source | notes |
|---|---|---|
| `geocode(place)` | Open-Meteo geocoding | up to 3 matches; error dict on no match |
| `climate_for_window(lat, lon, start_date, end_date)` | Open-Meteo archive | uses the same window **last year** as a proxy, shifted back another year if the archive lag would cut it off |
| `ocean_conditions(lat, lon)` | Open-Meteo marine | **live** 7-day outlook, not seasonal |

`run_tool()` **never raises**. Errors come back as `{"error": ...}`. HTTP timeout is 15 s. `mcp_server.py` exposes the same three tools by wrapping `run_tool`.

### HTTP API (`backend/app.py`)

| route | behavior |
|---|---|
| `GET /` | serves `frontend/index.html` |
| `GET /static/*` | frontend assets |
| `GET /api/health` | `{"ok": true, "live": bool(ANTHROPIC_API_KEY)}` |
| `GET /api/plan?prompt=<≤1000 chars>&demo=<bool>&speed=<float>` | SSE stream. Replays `demo/sample_run.jsonl` if `demo` is set or there's no key (`speed` floored at 0.1). Otherwise runs live. |

Live runs:
- Each run is gated by a semaphore (`TRADEWINDS_MAX_CONCURRENT`).
- Each run is recorded to `runs/<unix_ts>.jsonl`, with a `_delay` field on every event.
- A worker exception becomes an `error` event.
- A client disconnect cancels the worker task.

### Event protocol (backend ⇄ frontend ⇄ `demo/sample_run.jsonl`)

| type | emitted by | fields |
|---|---|---|
| `run_start` | crew | `prompt`, `agents: [{name, role}]` |
| `agent_start` | agent | `agent`, `role` |
| `thought` | agent | `agent`, `text` (≤400 chars) |
| `tool_call` | agent | `agent`, `tool`, `input` |
| `tool_result` | agent | `agent`, `tool`, `result` |
| `agent_done` | agent | `agent`, `seconds`, `output` |
| `agent_error` | agent | `agent`, `message` |
| `revision` | crew | `round` (1-based), `issues: [str]` |
| `final` | crew | `trip` (`FinalTrip`), `usage: {input_tokens, output_tokens}` |
| `error` | app | `message` |

Recorded files add `_delay` (seconds since the previous event). The replay strips it before sending.

## Project Structure

```
backend/            → server + agents (flat imports; run from here)
  agent.py          → Agent runtime loop, Usage
  schemas.py        → Pydantic contracts between agents
  crew.py           → agent definitions, CREW list, plan_trip()
  tools.py          → Open-Meteo tools, TOOL_REGISTRY, run_tool()
  app.py            → FastAPI + SSE, replay, run recording
  mcp_server.py     → MCP stdio server over the same tools
frontend/
  index.html        → entire UI (vanilla JS, switch on ev.type)
  vendor/           → Leaflet, fonts, marker images (vendored; don't edit)
demo/
  make_demo.py      → integration test + demo recorder (scripted fake model)
  sample_run.jsonl  → recording replayed in demo mode (generated)
runs/               → live-run recordings (gitignored, generated)
.claude/skills/     → installed Claude Code skills (not app code)
```

## Code Style

The patterns already in the codebase, which new code should match:

```python
# Contracts are Pydantic models with Field descriptions; the model reads them as the submit schema.
class Stay(BaseModel):
    base: str
    nights: int
    kind: str = Field(description="e.g. 'surf hostel private room', 'jungle cabina'")
    est_per_night_usd: int

# Tools are plain async functions that return dicts and turn failures into data, not exceptions.
async def run_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name not in TOOL_REGISTRY:
        return {"error": f"Unknown tool {name}"}
    ...
```

- `from __future__ import annotations` at the top of every module, with type hints on all signatures.
- Module docstring explaining *why* the module exists. Comments only for non-obvious intent.
- Agents are declared data (`Agent(...)`), and orchestration is plain `async`/`await` in `plan_trip`.
- Pass data between agents as values (`_j(model)`), not by mutating shared objects.
- `snake_case` functions and fields, `PascalCase` models, `UPPER_CASE` module constants (`MAX_REVISIONS`, `MODEL`).
- Frontend: vanilla JS, `$()` helper, escape every model-supplied string with `esc()` before inserting it as HTML.

## Testing Strategy

**V1 bar (as-built):**
- `python demo/make_demo.py` is the single required check. It drives the **real** `plan_trip` and `Agent.run` with a scripted fake model (`agent._client`) and stubbed tools (`agent.run_tool`), and asserts that:
  - the reviewer approved after exactly one revision, and day 5 became `pace == "slow"`
  - `sum(stay.nights) == brief.nights`
  - `sum(budget.lines.usd) == budget.total_usd`
- It needs no network and no API key. It **regenerates `demo/sample_run.jsonl`**, which must then be committed together with any change to the event shape.
- Coupling: `FakeMessages` identifies the agent by matching the `system` prompt against `crew.CREW`, then pops the next scripted response. Fake tool results are keyed by `(tool, place)` or `(tool, lat)`. Adding an agent or turn, changing a system prompt, or changing a schema requires updating `SCRIPTS` and the payloads.
- Manual check: start the server without a key, open the UI, and confirm the demo replays through to the final plan.

Pytest unit tests are proposed under Open Questions, not required for V1.

## Boundaries

- **Always:**
  - Keep the backend events, the frontend `switch (ev.type)` and `demo/sample_run.jsonl` in sync. Regenerate the demo after any event or schema change.
  - Keep the `run_tool` never-raises contract and the every-`tool_use`-gets-a-`tool_result` rule.
  - Run `python demo/make_demo.py` before considering a backend change done.
  - Escape model output in the frontend.
- **Ask first:**
  - Adding or changing a dependency in `requirements.txt`
  - Changing a field in `schemas.py`, which changes the submit schemas, the downstream prompts and the UI
  - Adding, removing or reordering agents, or changing `MAX_REVISIONS`
  - Changing the default model
  - Adding a frontend build step or framework
  - Adding a new external data source
- **Never:**
  - Introduce LangChain, LangGraph or any other agent framework
  - Commit API keys, `.env` or `runs/`
  - Edit `frontend/vendor/`
  - Hand-edit `demo/sample_run.jsonl` (regenerate it instead)
  - Weaken or delete the `make_demo.py` assertions to get a green run

## Success Criteria (V1 is "done" when all hold)

1. `pip install -r requirements.txt` succeeds on a clean Python 3.12 venv.
2. `python demo/make_demo.py` exits 0 and prints `demo recorded: Nicoya Peninsula 15 days` (14 nights, so 15 days from arrival to departure, matching the committed recording), offline and with no key.
3. With no key set, `GET /api/health` returns `live: false`, and `GET /api/plan` replays the full recording ending in a `final` event.
4. With a key set, a live run of the README example prompt:
   - emits `run_start` … `final`, in that order, with one `agent_start` and `agent_done` per agent invocation
   - writes `runs/<ts>.jsonl`
   - produces a `FinalTrip` whose itinerary has `brief.nights + 1` days (arrival through departure)
5. Closing the browser tab during a live run cancels the worker. No further model calls appear after disconnect.
6. With `TRADEWINDS_MAX_CONCURRENT=1`, a second simultaneous live run waits for the first to finish before starting.
7. `claude mcp add tradewinds -- python backend/mcp_server.py` exposes three tools, and `geocode("Nosara")` returns a match.
8. Every event type in the protocol table is handled by the frontend `switch`.

**Verification status (2026-11-14):** not yet verified in this environment. Python dependencies aren't installed (`ModuleNotFoundError: anthropic`), so criterion 2 couldn't be run.

## Known Gaps in V1 (found while reading the code)

These are documented as current behavior, not fixed by this spec. Each one is a candidate delta spec.

1. **Logistics can't see the itinerary.** It runs in parallel with the itinerary and is never revised, so stay `base`s and nights may not match the itinerary's bases, and only the reviewer would notice. Revisions only re-run the itinerary.
2. **Invariants live in prompts, not code.** `OptionScore.total == sum`, `Budget.total_usd == sum(lines)`, `sum(stays.nights) == brief.nights` and `nights == end - start` are asked of the model and asserted only in the demo, never validated in `schemas.py`.
3. **Feb 29 crash in `climate_for_window`.** `date.replace(year=last_year)` raises `ValueError` for a Feb 29 start or end. It comes back as a tool error, so it won't crash the run, but the model gets no data.
4. **The run log is opened before the semaphore.** Queued runs create empty `runs/*.jsonl` files, and `runs/` grows without limit.
5. **No spend limit.** Usage is counted but not capped per run or in total. The only guards are max_turns × agents and the concurrency semaphore.
6. **Demo replay ignores the prompt.** The UI shows the user's prompt, but the replayed content is always the Boston/Costa Rica run.
7. **Stale docstrings.** `crew.py` says "six specialist agents" (there are seven), and `schemas.py` says `submit_*` tools (the tool is named `submit`).
8. ~~**Not a git repository.**~~ Resolved 2026-11-14: initialized and pushed to GitHub.

## Out of Scope (Deferred)

**Stage 2: Demo (public URL)**
- Deployment to **Fly.io** (target chosen 2026-11-14). Deferred to the stage-2 delta spec.
- GitHub OAuth (or similar) gating live runs. Deferred because it's only needed once the site is public.
- Per-run and daily token or cost caps (gap 5). Deferred because the risk only exists with untrusted users.
- Rate limiting per IP or user. Same reason.
- Prompt caching on system prompts. It's a cost optimization that matters at public traffic.
- Langfuse (or similar) tracing. Useful once runs aren't watched live by the developer.

**Stage 3: Product**
- An eval set of about 20 prompts with constraint-violation checks. Needed before changing prompts or models with confidence.
- Persistence: saved trips, shareable links, history.
- User accounts.
- Real flight and lodging pricing APIs, replacing the model's estimates.
- A Claude Code plugin with a `/plan-trip` command.
- A pytest unit suite with a coverage target (see Open Questions).

## Open Questions

1. **Testing bar:** should V1 add a pytest suite now, covering `run_tool` error paths, the `Agent.run` validation-retry and skipped-result paths, the judge fallback and the climate date logic (80% coverage target per the global rules)? Or keep `make_demo.py` as the only gate until stage 2? *Default if unanswered: keep `make_demo.py` only for V1.*
2. ~~**Git:**~~ Resolved: the repo is on GitHub.
3. **Known gaps:** which, if any, should be fixed as V1.1 delta specs before stage 2? My suggested order is 1 (logistics/itinerary mismatch) → 2 (invariants) → 3 (Feb 29), with 4–5 folded into the stage-2 spec.
4. ~~**Stage 2 hosting:**~~ Decided: **Fly.io**. The stage-2 delta spec (`SPEC-fly-demo.md`) will cover the Dockerfile and `fly.toml`, secrets, where `runs/` lives (a volume or not recorded at all), and how SSE behaves behind Fly's proxy.
