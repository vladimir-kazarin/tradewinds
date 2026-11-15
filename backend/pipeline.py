"""Plain-async orchestration of the crew, in two stateless steps.

    plan_options:  conversation -> Brief -> (1-2 questions, up to 2 rounds
                                             | brief -> Scout -> candidates -> Judge -> 3 ranked options)
    plan_trip:     brief + option -> Itinerary -> Logistics -> Budget -> Reviewer
                                         ^                                 |
                                         +--- revise (MAX_REVISIONS) ------+

The browser carries the brief and chosen option between the two calls, so the
server keeps no session. Upstream outputs are passed as JSON text (`_j`).
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any

from agent import Emit, Usage
from checks import (base_sequence, country_mismatches, itinerary_mismatches, option_budget_issues,
                    plan_budget_issues, stay_country_mismatches, stay_mismatches)
from crew import (CREW, brief_agent, brief_decide_agent, budget_agent, itinerary_agent, judge_agent,
                  logistics_agent, reviewer_agent, scout_agent)
from links import build_links
from schemas import (ChatTurn, DestinationOption, Itinerary, Logistics, PlanResult, RankedOption, ScoutReport,
                     TripBrief, TripLinks, VibeVerdict)
from status import with_status

MAX_REVISIONS = 1  # one fix round: a second rarely changed the outcome and doubled the wait
MAX_QUESTION_ROUNDS = 2
MAX_NOTES = 5
STEP_ONE_AGENTS = ("brief", "scout", "judge")


def _j(m: Any) -> str:
    return m.model_dump_json(indent=1) if hasattr(m, "model_dump_json") else json.dumps(m, indent=1)


def _dump(m: Any) -> Any:
    return json.loads(m.model_dump_json())


def render_transcript(turns: list[ChatTurn]) -> str:
    lines = [f"Today is {date.today()}.", ""]
    lines += [f"{'Traveler' if t.role == 'user' else 'You asked'}: {t.text}" for t in turns]
    return "\n".join(lines)


def rank_options(scout: ScoutReport, verdict: VibeVerdict) -> list[RankedOption]:
    by_name = {o.name.casefold(): o for o in scout.options}
    ordered: list[DestinationOption] = []
    for name in verdict.ranking:
        o = by_name.get(name.casefold())
        if o is not None and o not in ordered:
            ordered.append(o)
    ordered += [o for o in scout.options if o not in ordered]
    pick = by_name.get(verdict.pick.casefold())
    if pick is not None:
        ordered = [pick] + [o for o in ordered if o is not pick]
    pitches = {p.name.casefold(): p.pitch for p in verdict.pitches}
    return [RankedOption(option=o, pitch=pitches.get(o.name.casefold(), o.why), best=i == 0)
            for i, o in enumerate(ordered)]


async def _emit_usage(emit: Emit, usage: Usage) -> None:
    await emit({"type": "usage", "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
                "cost_usd": round(usage.cost_usd, 4)})


def _run_start(step: str) -> dict[str, Any]:
    return {"type": "run_start", "step": step, "agents": [{"name": a.name, "role": a.role} for a in CREW]}


async def plan_options(turns: list[ChatTurn], emit: Emit, usage: Usage | None = None) -> dict[str, Any]:
    """Step 1. Returns the terminal event it emitted (`questions` or `options`).

    Pass a `Usage` carrying the run's spend cap and the daily ledger; the default has no limits."""
    emit = with_status(emit)
    usage = usage or Usage()
    await emit(_run_start("options"))
    task = render_transcript(turns)
    rounds_asked = sum(t.role == "assistant" for t in turns)
    if rounds_asked >= MAX_QUESTION_ROUNDS:
        decision = await brief_decide_agent.run(task, emit, usage)
        reply, brief, followups = decision.reply, decision.brief, decision.followups
    else:
        result = await brief_agent.run(task, emit, usage)
        if result.questions:
            ev = {"type": "questions", "reply": result.reply, "questions": result.questions}
            await emit(ev)
            await _emit_usage(emit, usage)
            return ev
        reply, brief, followups = result.reply, result.brief, result.followups
    # Show the trip summary (and optional follow-ups) right away; the search runs meanwhile.
    await emit({"type": "brief", "reply": reply, "brief": _dump(brief), "followups": followups})

    scout = await scout_agent.run(f"Brief:\n{_j(brief)}", emit, usage)
    over = option_budget_issues(brief, scout)
    if over:  # one retry; never show a card the traveler can't afford
        scout = await scout_agent.run(
            f"Brief:\n{_j(brief)}\n\nYour previous options:\n{_j(scout)}\n\nFix these problems:{_issues_text(over)}",
            emit, usage)
    await emit({"type": "candidates", "options": [_dump(o) for o in scout.options]})  # cards before ranking
    verdict = await judge_agent.run(f"Brief:\n{_j(brief)}\n\nOptions:\n{_j(scout)}", emit, usage)
    ranked = rank_options(scout, verdict)
    ev = {"type": "options", "reply": reply, "brief": _dump(brief), "options": [_dump(r) for r in ranked]}
    await emit(ev)
    await _emit_usage(emit, usage)
    return ev


def _issues_text(issues: list[str]) -> str:
    return "\n- " + "\n- ".join(issues)


def _itinerary_issues(brief: TripBrief, option: DestinationOption, itinerary: Itinerary) -> list[str]:
    return itinerary_mismatches(brief, itinerary) + country_mismatches(option, itinerary)


async def _itinerary(ctx: str, brief: TripBrief, option: DestinationOption, emit: Emit, usage: Usage,
                     previous: Itinerary | None = None, review_issues: list[str] | None = None) -> Itinerary:
    """Build (or revise, given previous + review_issues) the itinerary, with one code-check retry."""
    task = ctx
    if previous is not None:
        task = (f"{ctx}\n\nYour previous itinerary:\n{_j(previous)}\n\n"
                f"The reviewer found these issues, fix all of them:{_issues_text(review_issues or [])}")
    itinerary = await itinerary_agent.run(task, emit, usage)
    issues = _itinerary_issues(brief, option, itinerary)
    if issues:
        itinerary = await itinerary_agent.run(
            f"{ctx}\n\nYour previous itinerary:\n{_j(itinerary)}\n\nFix these problems:{_issues_text(issues)}",
            emit, usage)
    return itinerary


async def _logistics(ctx: str, brief: TripBrief, option: DestinationOption, itinerary: Itinerary,
                     emit: Emit, usage: Usage) -> Logistics:
    task = f"{ctx}\n\nItinerary:\n{_j(itinerary)}"
    logistics = await logistics_agent.run(task, emit, usage)
    issues = stay_mismatches(itinerary, logistics, brief.nights) + stay_country_mismatches(option, logistics)
    if issues:
        # One retry; anything left is the reviewer's to catch.
        logistics = await logistics_agent.run(
            f"{task}\n\nYour previous logistics:\n{_j(logistics)}\n\nFix these problems:{_issues_text(issues)}",
            emit, usage)
    return logistics


async def _emit_logistics(emit: Emit, brief: TripBrief, itinerary: Itinerary, logistics: Logistics) -> None:
    await emit({"type": "plan_part", "part": "logistics", "logistics": _dump(logistics),
                "links": _dump(final_links(brief, itinerary, logistics))})


def final_links(brief: TripBrief, itinerary: Itinerary, logistics: Logistics) -> TripLinks:
    """Stay links come from the itinerary's own nights, so they can't name a town it doesn't sleep in."""
    return build_links(brief, logistics.flights, base_sequence(itinerary, brief.nights))


async def plan_trip(brief: TripBrief, option: DestinationOption, emit: Emit,
                    usage: Usage | None = None, notes: list[str] | None = None) -> PlanResult:
    """Step 2: fully plan one option. `notes` are things the traveler added while the search ran;
    each finished part is emitted as a `plan_part` so the UI can show it before the review."""
    emit = with_status(emit)
    usage = usage or Usage()
    await emit(_run_start("plan"))
    for name in STEP_ONE_AGENTS:
        await emit({"type": "agent_skip", "agent": name})

    ctx = f"Brief:\n{_j(brief)}\n\nChosen trip:\n{_j(option)}"
    if notes:
        ctx += "\n\nThe traveler also said (these override the brief where they conflict):" + _issues_text(
            notes[:MAX_NOTES])
    itinerary = await _itinerary(ctx, brief, option, emit, usage)
    await emit({"type": "plan_part", "part": "itinerary", "itinerary": _dump(itinerary)})
    logistics = await _logistics(ctx, brief, option, itinerary, emit, usage)
    await _emit_logistics(emit, brief, itinerary, logistics)

    for round_ in range(MAX_REVISIONS + 1):
        plan_ctx = f"{ctx}\n\nItinerary:\n{_j(itinerary)}\n\nLogistics:\n{_j(logistics)}"
        budget = await budget_agent.run(plan_ctx, emit, usage)
        await emit({"type": "plan_part", "part": "budget", "budget": _dump(budget)})
        known = stay_mismatches(itinerary, logistics, brief.nights) + plan_budget_issues(brief, budget)
        flagged = f"\n\nAutomated checks found:{_issues_text(known)}" if known else ""
        review = await reviewer_agent.run(f"{plan_ctx}\n\nBudget:\n{_j(budget)}{flagged}", emit, usage)
        if review.approved or round_ == MAX_REVISIONS:
            break
        await emit({"type": "revision", "round": round_ + 1, "issues": review.issues})
        itinerary = await _itinerary(ctx, brief, option, emit, usage, itinerary, review.issues)
        await emit({"type": "plan_part", "part": "itinerary", "itinerary": _dump(itinerary)})
        logistics = await _logistics(ctx, brief, option, itinerary, emit, usage)
        await _emit_logistics(emit, brief, itinerary, logistics)

    result = PlanResult(option_name=option.name, itinerary=itinerary, logistics=logistics, budget=budget,
                        review=review, links=final_links(brief, itinerary, logistics))
    await emit({"type": "plan", **_dump(result)})
    await _emit_usage(emit, usage)
    return result
