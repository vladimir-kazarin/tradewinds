# Delta spec: Product flow (chat → 3 options → plan on demand)

Status: implemented (2026-11-14) on branch `feat/product-flow`
Baseline: `SPEC.md` (V1 as-built). This document only describes what changes.

## Intent

Tradewinds is now a consumer product, not a tech demo. The V1 UI reads like a debugging console: agent thoughts, tool calls and token counts, all on one overloaded page. Real travelers want to say where they'd like to go, answer a couple of questions, see three good trips with a price each, and dig into the one they like.

**Success:** a non-technical person types one sentence, answers at most one round of follow-up questions, and within a minute or so sees three distinct trips with a rough total cost each. Clicking one gives a reviewed day-by-day plan, a cost breakdown and links that open real flight and stay searches.

## Goals

- First screen: just "Where would you like to go?" and an input. Nothing else.
- The Brief agent may ask one round of follow-up questions (dates, origin, budget, events/places, multiple countries) or skip it if the prompt is complete.
- While agents work: a spinning globe with the seven crew members in a **ring** around it (chosen over a linear flow), plus one friendly status line.
- Results: **3 cards**, each with destination, one-line pitch, "Best match" badge on the judge's pick, and a **rough total budget** labelled as an estimate.
- Clicking a card ("Plan this trip") runs the planning half of the crew for that option only, then expands to itinerary, map, real budget breakdown, and Flights/Stays buttons. Already planned cards stay planned.

## Non-goals

- Accounts, saved trips, shareable links, history (stage 3).
- Live prices or booking through an API. Links are prefilled searches.
- Changing the visual identity. Keep the existing palette (`--sand`, `--paper`, `--ink`, `--lagoon`, `--coral`, `--sun`, …) and fonts (Fraunces + system/vendored).
- Deployment to Fly.io (separate delta spec).

## Anti-goals (failure even if it "works")

- Any tool name, raw model thought, JSON, score number or token count visible to the end user.
- The Brief agent turning into an interrogation (more than one question round).
- A booking link pointing to a town the itinerary doesn't sleep in, or a URL the model invented.
- Planning all three trips fully up front (cost and wait the user didn't ask for).

## Approaches considered

| | Approach | Trade-off | Decision |
|---|---|---|---|
| 1 | **Scout estimates rough cost per option; browser carries state between steps** | Rough number is one agent's estimate, but it's replaced by the full plan. Cheapest, fastest, no storage. | **Chosen** |
| 2 | Separate quick-budget agent per option after the judge | Better estimate, more cost and latency for a number that gets replaced | Rejected |
| 3 | Server-side session per search, step 2 fetches by id | Starts persistence early, but adds storage now and needs sticky routing on Fly.io; saved trips are deferred | Rejected (revisit in stage 3) |

Also decided in conversation: plan all 3 fully up front was rejected in favour of options first and full plan on click, with a budget for each option shown up front.

## Multi-country trips

A trip can span several countries, and the three options can mix single- and multi-country trips (e.g. "Nicoya Peninsula" and "Caribbean Costa Rica + Bocas del Toro, Panama").

- `TripBrief` gains `countries_wanted: str` ("one", "several" or "either"), filled from the Brief's question or a sensible default ("either").
- `DestinationOption.country: str` becomes `countries: list[str]` (min 1, ordered as visited). `lat/lon` stays the map centre. Cards show the countries joined with " · ".
- `DayPlan` and `Stay` gain `country: str`. Validator: every day's and stay's country must be in the option's `countries` (checked in code with the option in hand, fed back to the agent on mismatch).
- Travel between countries: `Logistics` gains `transfers: list[Transfer]` (`from_base`, `to_base`, `mode` e.g. "flight"/"ferry"/"bus", `est_usd`). Budget must include them as a line. The itinerary gives a transfer day a slow pace.
- Flights are open-jaw when the last base is in a different country than the first (`return_from_airport` differs from `arrive_airport`).
- Stays links use each stay's own `base, country`. The Scout's rough cost includes inter-country transfers in its `transfers_usd` field.

## Agent system design

```
 STEP 0 · chat              STEP 1 · options                 STEP 2 · plan (on click, per card)
 conversation ─► Brief ─┬─► Scout ──────► Judge ─► 3 cards   brief + option ─► Itinerary ─► Logistics
                        │   geocode,      rank +               (ocean)                          │
                        │   climate,      pitch                                                 ▼
                        │   rough cost                                       Budget ─► Reviewer ─┐
                        └─► questions (max 1 round)                            ▲    revise ≤ 2   │
                                                                               └─────────────────┘
```

Steps 0 and 1 are one request: the Brief either ends the stream with questions, or the run continues into Scout and Judge.

### Brief (changed)
- Input: the whole conversation, as alternating user/assistant turns rendered into the task text (today's date included as now).
- Output: a new model `BriefResult` with `reply: str` (friendly one or two sentences shown in chat), `questions: list[str]` (empty when deciding), `brief: TripBrief | None` (set when deciding). Exactly one of non-empty `questions` / `brief` must be present; enforce with a Pydantic validator so a bad submit bounces back to the model.
- Asks only about what's missing and would change the trip: dates/length, origin, budget, must-see events or places, one vs several countries.
- **One round max, enforced by the orchestrator:** if the conversation already contains an assistant turn with questions, the task text tells it to decide now, and `questions` must be empty (validator receives this via the task, orchestrator re-checks; if the model still returns questions on round 2, treat as a hard error and emit `error`).
- System prompt drops "Never ask questions; decide" and replaces it with the rules above.

### Scout (changed)
- Returns **exactly 3** options (`ScoutReport.options` min 3, max 3).
- Each `DestinationOption` gains `rough_cost: RoughCost` with `flights_usd`, `lodging_usd` (whole stay), `daily_spend_usd` (whole stay: food, activities, local transport), `transfers_usd` (between countries/regions, 0 if none), `total_usd`. Validator: `total_usd` equals the sum of the four.
- May propose multi-country options when `countries_wanted` is "several" or "either"; must propose at least one when "several".
- System prompt: give mid-range estimates for the brief's dates, origin and nights; say they're estimates.

### Judge (changed slightly)
- Unchanged scoring. `VibeVerdict` gains `pitches: list[OptionPitch]` (`name`, `pitch` ≤ ~140 chars, warm, user-facing) and `ranking: list[str]` (names, best first).
- Fallbacks in code: unmatched `pick` → first option (as today); missing/invalid ranking → pick first then scout order; missing pitch → option's `why`.

### Itinerary → Logistics (changed: sequential)
- Itinerary runs first (tools: `geocode`, `ocean_conditions`), as today.
- Logistics then runs with the itinerary in its input and must produce **one stay per distinct consecutive base**, with base names copied from the itinerary and nights summing to `brief.nights`.
- `FlightPlan` gains `depart_airport` and `arrive_airport` (3-letter IATA codes, validated `^[A-Z]{3}$`) and `return_from_airport` (same; differs from `arrive_airport` only for open-jaw). The flights link uses these, never `route` or `option.name`.
- Code check after Logistics: the order of stay `(base, country)` pairs equals the itinerary's base sequence and nights sum matches. On mismatch, re-run Logistics once with the specific mismatch as feedback; if it still fails, carry on and let the Reviewer flag it.
- Fixes V1 known gap 1.

### Budget and Reviewer (mostly unchanged)
- Budget: `total_usd == sum(lines.usd)` enforced by a Pydantic validator (fixes part of gap 2).
- Revision loop (`MAX_REVISIONS = 2`): re-run Itinerary with reviewer issues, then **re-run Logistics** against the new itinerary, then Budget + Reviewer.

### Done by code, not the model
- **Booking links**, built server-side and attached to the plan result:
  - Flights: Google Flights search `depart_airport` → `arrive_airport` on `start_date`, return `return_from_airport` → `depart_airport` on `end_date`.
  - Stays: one Booking.com search per stay, for `base, country`, check-in/out from cumulative nights.
  - Only query parameters from validated fields, URL-encoded. The model never writes URLs.
- **Status line**: a deterministic mapping from events to friendly sentences, e.g. `agent_start scout` → "Looking for places that fit…", `tool_call climate_for_window` with a geocoded name → "Checking December weather near Nosara…", `revision` → "Polishing a few details…". Emitted as a `status` event. No tokens spent.
- Arithmetic invariants (above) as validators so the model fixes them itself.

## HTTP API

| route | change |
|---|---|
| `POST /api/options` | **new.** Body `{messages: [{role: "user"\|"assistant", text}]}` (≤ 8 turns, each ≤ 1000 chars, last must be user). SSE stream of steps 0–1. Ends with `questions` or `options`. |
| `POST /api/plan` | **changed** from GET. Body `{brief: TripBrief, option: DestinationOption}` validated by Pydantic. SSE stream of step 2. Ends with `plan`. |
| `GET /api/health` | unchanged |

`EventSource` can't POST, so the frontend reads SSE with `fetch` + a `ReadableStream` reader (small parser in vanilla JS, no library). The server stores nothing between calls (approach 1). The semaphore, run recording and disconnect cancellation apply to both streaming routes. Known gap 4 (log file opened before the semaphore) is fixed while touching `_live`: open the log after acquiring the slot.

**Demo mode** (no key, or `demo=1` query): `/api/options` replays `demo/questions_run.jsonl` when the transcript has no assistant turn yet, else `demo/options_run.jsonl`; `/api/plan` replays `demo/plan_run.jsonl` (recorded for the best-match option). Planning a non-recorded option in demo mode replays the same plan recording; acceptable for a demo, and noted in the UI only in demo mode ("Demo: showing a sample plan").

## Event protocol (changes)

| type | status | fields |
|---|---|---|
| `run_start` | changed | `step: "options"\|"plan"`, `agents: [{name, role}]` (all seven, always) |
| `agent_start`, `agent_done`, `agent_error` | unchanged | drive the ring |
| `status` | **new** | `text` (friendly sentence) |
| `questions` | **new** | `reply`, `questions: [str]` (terminal for step 0) |
| `options` | **new** | `reply` (Brief's friendly line, shown as a chat bubble before the cards), `brief`, `options: [{option, pitch, rough_cost, best: bool}]` in ranking order (terminal for step 1) |
| `plan` | **new**, replaces `final` | `option_name`, `itinerary`, `logistics`, `budget`, `review`, `links: {flights: url, stays: [{base, url}]}` |
| `revision` | unchanged | (UI only shows a status line for it) |
| `thought`, `tool_call`, `tool_result` | kept on the wire and in recordings | **ignored by the product UI** |
| `final` | removed | |
| `error` | unchanged | UI shows a friendly message + "Try again" |

Usage totals move to the run log (`runs/*.jsonl`) only; not sent to the browser.

## Frontend (`frontend/index.html`, rewritten in place)

Still one file, vanilla JS, vendored Leaflet and fonts, no build step, `esc()` on every model string.

1. **Ask:** centred serif "Where would you like to go?" and a pill input with a send button.
2. **Chat:** user and assistant bubbles. A `questions` event adds the assistant bubble (reply + questions as a list) and re-enables input. The browser keeps the transcript and sends it whole on each `/api/options` call.
3. **Working:** CSS spinning globe (as in `docs/mockups/product-flow.html`, option A), seven nodes evenly on a ring around it with emoji + short label (Brief, Scout, Judge, Itinerary, Logistics, Budget, Reviewer). States: idle / now (coral halo) / done (lagoon). Arc fills as nodes finish. One italic status line from `status` events. In step 2 the first three are already done.
4. **Results:** 3 cards (best match first, badged), hero gradient, destination + country + nights, pitch, "about $X" rough total with "estimate" label, "Plan this trip" button.
5. **Planned card:** the working view runs (inline over the card area or as an overlay, builder's call), then the card expands to: headline, day list, Leaflet map of bases, budget breakdown (replaces the rough figure), Flights button, one Stays button per base, a "within budget" note from Budget. Reviewer summary and scores stay hidden.
6. **Error:** friendly message, "Try again" re-sends the last request.

Mobile-first: cards stack at phone width; ring scales down.

## Testing

- `demo/make_demo.py` grows to drive both steps against the real orchestrator with the scripted fake model, and writes all three recordings (`questions_run`, `options_run`, `plan_run`). Assertions:
  - step 0 with the vague prompt returns `questions`; a second call with the answer appended returns `options` (proves the one-round rule path).
  - exactly 3 options, each `rough_cost.total_usd` equals its parts, best-match first.
  - step 2 for the best option: reviewer approves after exactly one revision, logistics re-ran after the revision, stay bases match itinerary bases, `sum(stay.nights) == brief.nights`, `sum(budget.lines) == budget.total_usd`.
  - every link is `https://` on the expected host and contains the URL-encoded base names.
  - the assertions from V1 are kept, not weakened.
- Add `tests/` with pytest for pure code paths that are now product-critical (link builder incl. multi-country/open-jaw, status mapping, validators, judge fallbacks). pytest goes in a new `requirements-dev.txt` (approved 2026-11-14); `requirements.txt` is unchanged. Update README and CLAUDE.md commands with `pytest`.
- The scripted demo includes one multi-country option, and the planned recording uses a single-country trip; a pytest case covers multi-country links and transfers.
- Manual: with no key, the UI replays both steps end to end.

## Constraints and boundaries

- No agent framework. Raw SDK and `Agent.run` stay as they are; only agent definitions, schemas and `crew.py` orchestration change.
- `run_tool` never raises; every `tool_use` gets a `tool_result`.
- Schema, agent-order and event changes are approved by this spec. New runtime dependencies still need asking. pytest in `requirements-dev.txt` is approved.
- Regenerate recordings, never hand-edit them.

## Calls made while writing (check these)

- Flight links use validated IATA airport fields on `FlightPlan`, not free text.
- Demo mode uses three recordings so a no-key user sees the question round, then the cards, then a plan.
- The Brief's `reply` is shown in both cases (with questions, and above the cards).
- Multi-country is in scope (decided by the user): `countries` list on options, `country` on days and stays, `transfers` on logistics.
- The mockup from brainstorming lives at `docs/mockups/product-flow.html` as the visual reference.

## Left to the builder

- Exact status-line wording and the mapping table.
- Whether the step-2 working view is an overlay or inline in the card.
- Card hero gradients per option, emoji choice per crew member.
- How `crew.py` is split (e.g. `plan_options()` and `plan_trip(brief, option)`), keeping files small.
- SSE-over-fetch parser details.
