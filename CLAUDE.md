# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Tradewinds is a multi-agent trip planner. A free-form prompt goes through a fixed pipeline of seven Claude agents, and every step is streamed to a single-page UI over SSE. It is written directly on the raw Anthropic SDK with no agent framework. That is a deliberate choice (see README "Design decisions"), so don't introduce LangChain/LangGraph or similar.

## Commands

```bash
pip install -r requirements.txt

# Run the server (must run from backend/, since modules use flat imports like `from tools import ...`)
cd backend && uvicorn app:app --reload --port 8000     # http://localhost:8000

# Integration test + regenerate the demo recording (no API key, no network)
python demo/make_demo.py

# Expose the tools over MCP
claude mcp add tradewinds -- python backend/mcp_server.py
```

There is no pytest suite, linter config, or build step. `demo/make_demo.py` is the only test: it runs the real orchestrator with a scripted fake model and stubbed tools, then asserts on the result (reviewer approved after one revision, stay nights sum to brief nights, budget lines sum to total). **It overwrites `demo/sample_run.jsonl`**, which is the file the UI replays in demo mode.

Env vars: `ANTHROPIC_API_KEY` (if unset, `/api/plan` always replays the demo), `TRADEWINDS_MODEL` (default `claude-sonnet-5-5`), `TRADEWINDS_MAX_CONCURRENT` (default 2).

## Architecture

```
Brief → Scout → Vibe Judge → (Itinerary ‖ Logistics) → Budget → Reviewer
                                  ↑                                  │
                                  └──── revise, MAX_REVISIONS=2 ─────┘
```

- **`backend/agent.py`**: the whole runtime. `Agent.run()` loops over model calls with `tool_choice: any`, and on the final turn it forces `submit`. Every agent finishes by calling a synthetic `submit` tool whose `input_schema` is `output.model_json_schema()`. If Pydantic validation fails, the error goes back to the model as an `is_error` tool_result. Real tool calls in a turn run concurrently. Every `tool_use` must get a `tool_result`, including skipped ones.
- **`backend/schemas.py`**: Pydantic models that serve as the contracts between agents. Changing a field changes the submit schema the model sees, the JSON passed to downstream agents, and what the frontend renders.
- **`backend/crew.py`**: agent definitions (system prompt, tools, output model, max_turns) and `plan_trip()`, plain async orchestration. Agents receive upstream outputs as JSON text in the user message (`_j()`), not as shared state. If the judge's `pick` doesn't match a scout option name, it falls back to the first option.
- **`backend/tools.py`**: Open-Meteo tools (geocode, historical climate proxy, live marine). `TOOL_REGISTRY` maps name → (async fn, Anthropic tool schema). `run_tool()` never raises; errors come back as `{"error": ...}` dicts so the model can recover. `agent.py` marks a result as `is_error` when it contains an `"error"` key. `mcp_server.py` wraps the same `run_tool` (mcp>=2 `MCPServer` API).
- **`backend/app.py`**: FastAPI + `sse-starlette`. `emit(ev)` is the single event channel. Live runs write each event to `runs/<ts>.jsonl` with a `_delay` field (gitignored), and the replay endpoint uses that to reproduce timing. A semaphore limits concurrent live runs, and client disconnect cancels the worker task.
- **`frontend/index.html`**: a single file of vanilla JS with vendored Leaflet and fonts. It consumes `EventSource("/api/plan?...")` and dispatches on `ev.type`.

### Event protocol

Event types emitted are `run_start`, `agent_start`, `thought`, `tool_call`, `tool_result`, `agent_done`, `agent_error`, `revision`, `final`, and `error`. The backend (`agent.py`, `crew.py`, `app.py`) and the frontend `switch (ev.type)` must stay in sync, and so must the recorded `demo/sample_run.jsonl`. If you change an event's shape, regenerate the demo.

### Demo script coupling

`demo/make_demo.py` monkeypatches `agent._client` and `agent.run_tool`. `FakeMessages` identifies which agent is calling by **matching the `system` prompt against `crew.CREW`**, then pops the next scripted response from that agent's list in `SCRIPTS`. Fake tool results are keyed by `(tool, place)` or `(tool, lat)`. As a result:
- Adding an agent, adding a turn, or changing the revision flow requires matching entries in `SCRIPTS`. Otherwise the script hits `StopIteration` or a missing key.
- Schema changes require updating the scripted submit payloads (`BRIEF`, `OPTS`, `ITIN`, and so on), or validation fails in a loop until max_turns.
