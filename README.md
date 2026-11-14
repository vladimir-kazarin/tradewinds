# Tradewinds 🌴

**Describe how you feel, and a crew of seven AI agents plans the trip while you watch.**

> "Two weeks from Dec 1, burnt out, want chill + tropical, learn to surf, great food, no party scene. From Boston, ~$3k."

That prompt turns into a scored shortlist of destinations, a day-by-day itinerary on a map, flights and stays, a budget, and a reviewer sign-off. Every agent step and tool call streams live to the UI.

## Run it

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...        # leave unset to run the recorded demo
cd backend && uvicorn app:app --reload --port 8000
# open http://localhost:8000
```

Optional settings: `TRADEWINDS_MODEL` (default `claude-sonnet-5-5`) and `TRADEWINDS_MAX_CONCURRENT` (default 2 live runs).

## Architecture

```
prompt ─► Brief ─► Scout ─► Vibe Judge ─► ┬─ Itinerary ─┬─► Budget ─► Reviewer ─► final
                    │ tools                └─ Logistics ─┘                │
                    │ geocode, climate          (parallel)                │ rejected?
                    ▼                                                     ▼
              Open-Meteo APIs                         Itinerary revises (max 2 rounds)
```

| Piece | File | What it shows |
|---|---|---|
| Agent runtime | `backend/agent.py` | Tool-use loop written directly on the raw Anthropic SDK (no framework). Tools run in parallel, tool errors go back to the model instead of being raised, and there's a max-turns limit that forces `submit` on the last turn. |
| Structured output | `backend/schemas.py` | Each agent finishes by calling a `submit` tool whose schema is a Pydantic model. Invalid output is sent back to the model with the validation error. |
| Orchestration | `backend/crew.py` | Plain Python async. Fixed pipeline steps, a parallel fan-out with `asyncio.gather`, and a reviewer → revision feedback loop. |
| Tools | `backend/tools.py` | Real data: geocoding, historical climate for the travel dates, and a live wave outlook (Open-Meteo, no API key). |
| MCP server | `backend/mcp_server.py` | The same tools served over MCP: `claude mcp add tradewinds -- python backend/mcp_server.py` |
| Streaming | `backend/app.py` | FastAPI + SSE. Live runs are recorded as JSONL and can be replayed. A semaphore caps concurrent runs, and a client disconnect cancels the run so it stops using tokens. |
| Demo / test | `demo/make_demo.py` | Runs the real orchestrator with a scripted fake model. It's an integration test of the loop, the parallel step and the revision step, and it also produces the demo recording. |
| UI | `frontend/index.html` | One page: crew roster, live tool feed, scored destination cards, Leaflet map, day timeline with "revised" badges, budget breakdown. |

## Design decisions (interview notes)

- **Why no framework?** The loop is about 80 lines, and owning it makes retries, validation, cost tracking and termination easy to see and control. LangGraph is worth adding when you need durable checkpoints and human-in-the-loop resume.
- **Why a fixed pipeline and not an LLM router?** The order of steps is known in advance, so the code handles control flow and the model handles judgment. That makes runs cheaper, deterministic, and easier to debug and evaluate.
- **Why submit-as-a-tool?** One mechanism covers both tool calls and the final answer, the output is schema-validated, and the model gets a chance to fix its own errors.
- **Hallucination guardrails:** the scout has to call `climate_for_window` before it says anything about weather, the judge scores against a fixed rubric, and the reviewer checks hard constraints (night counts, budget total, avoid-list, transfers that aren't realistic).
- **Next steps:** an eval set of about 20 prompts with checks on constraint violations, Langfuse tracing, prompt caching on system prompts, GitHub OAuth for live runs on a public site, and a Claude Code plugin with a `/plan-trip` command.
