"""The crew: six specialist agents wired together by a plain-Python orchestrator.

    prompt -> Brief -> Scout -> Vibe Judge -> (Itinerary || Logistics) -> Budget -> Reviewer
                                                  ^                                    |
                                                  +------ revise (max 2 rounds) -------+
"""
from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import Any

from agent import Agent, Emit, Usage
from schemas import (Budget, FinalTrip, Itinerary, Logistics, Review, ScoutReport,
                     TripBrief, VibeVerdict)

MAX_REVISIONS = 2

brief_agent = Agent(
    name="brief", role="Trip Brief", output=TripBrief, max_turns=2,
    system=(
        "You turn a traveler's free-form, emotional prompt into a precise trip brief. "
        "Resolve relative dates against today's date. If something is unstated, choose a sensible default "
        "and put the assumption in travel_style. Never ask questions; decide."
    ),
)

scout_agent = Agent(
    name="scout", role="Destination Scout", output=ScoutReport,
    tools=["geocode", "climate_for_window"],
    system=(
        "You are a well-traveled destination scout. Propose 3 genuinely different destinations that fit the "
        "brief's vibe and budget, reachable from the origin. For each, geocode it and check climate_for_window "
        "for the exact travel dates; the weather_summary must come from tool data, not memory. "
        "Prefer places that feel calm and uncrowded over famous party spots unless asked otherwise."
    ),
)

judge_agent = Agent(
    name="judge", role="Vibe Judge", output=VibeVerdict, max_turns=2,
    system=(
        "You are an impartial judge. Score each destination 1-10 on vibe_fit, weather, budget_fit and ease "
        "(flight time, connections, getting around), total = sum. Pick the highest total; break ties on vibe_fit. "
        "Be specific in your reasoning and mention what the runner-up does better."
    ),
)

itinerary_agent = Agent(
    name="itinerary", role="Itinerary Builder", output=Itinerary,
    tools=["geocode", "ocean_conditions"], max_tokens=8000,
    system=(
        "You build day-by-day itineraries that match the traveler's mood. Use 2-3 bases max for slow travel; "
        "every day needs a real base town with coordinates (geocode each base once). Leave genuinely empty, "
        "restful time in the plan. If the trip involves the ocean, check ocean_conditions once per coastal base. "
        "Day 1 is arrival, the last day is departure. Write vivid but concrete activities."
    ),
)

logistics_agent = Agent(
    name="logistics", role="Logistics", output=Logistics, max_turns=2,
    system=(
        "You plan flights, lodging and ground transport. Give realistic mid-range USD estimates for the travel "
        "dates and say they're estimates. Match lodging style to the vibe. Use the bases from the destination "
        "notes; nights across stays must sum to the trip's nights."
    ),
)

budget_agent = Agent(
    name="budget", role="Budget", output=Budget, max_turns=2,
    system=(
        "You are a frugal accountant. Build a budget from the logistics plus food, activities and a 10% buffer. "
        "total_usd must equal the sum of lines. Set within_budget honestly; if over, say exactly what to cut."
    ),
)

reviewer_agent = Agent(
    name="reviewer", role="Reviewer", output=Review, max_turns=2,
    system=(
        "You are a strict QA reviewer. Check the plan against the brief: dates and day count match, nights "
        "line up with stays, budget respected, must-haves present, avoid-list respected, pace matches the vibe, "
        "no impossible transfers (e.g. 6h drive on a 'slow' day). Approve only if there are no real issues. "
        "Issues must be concrete and fixable by the itinerary builder."
    ),
)

CREW = [brief_agent, scout_agent, judge_agent, itinerary_agent, logistics_agent, budget_agent, reviewer_agent]


def _j(m: Any) -> str:
    return m.model_dump_json(indent=1) if hasattr(m, "model_dump_json") else json.dumps(m, indent=1)


async def plan_trip(prompt: str, emit: Emit) -> FinalTrip:
    usage = Usage()
    await emit({"type": "run_start", "prompt": prompt,
                "agents": [{"name": a.name, "role": a.role} for a in CREW]})

    brief = await brief_agent.run(f"Today is {date.today()}.\n\nTraveler says:\n{prompt}", emit, usage)

    scout = await scout_agent.run(f"Brief:\n{_j(brief)}", emit, usage)

    verdict = await judge_agent.run(f"Brief:\n{_j(brief)}\n\nOptions:\n{_j(scout)}", emit, usage)
    dest = next((o for o in scout.options if o.name.lower() == verdict.pick.lower()), scout.options[0])
    alts = [o for o in scout.options if o is not dest]

    ctx = f"Brief:\n{_j(brief)}\n\nChosen destination:\n{_j(dest)}"
    # Independent work runs in parallel.
    itinerary, logistics = await asyncio.gather(
        itinerary_agent.run(ctx, emit, usage),
        logistics_agent.run(ctx, emit, usage),
    )

    for round_ in range(MAX_REVISIONS + 1):
        budget = await budget_agent.run(
            f"{ctx}\n\nLogistics:\n{_j(logistics)}\n\nItinerary:\n{_j(itinerary)}", emit, usage)
        review = await reviewer_agent.run(
            f"{ctx}\n\nItinerary:\n{_j(itinerary)}\n\nLogistics:\n{_j(logistics)}\n\nBudget:\n{_j(budget)}",
            emit, usage)
        if review.approved or round_ == MAX_REVISIONS:
            break
        await emit({"type": "revision", "round": round_ + 1, "issues": review.issues})
        itinerary = await itinerary_agent.run(
            f"{ctx}\n\nYour previous itinerary:\n{_j(itinerary)}\n\n"
            f"The reviewer found these issues, fix all of them:\n- " + "\n- ".join(review.issues),
            emit, usage)

    trip = FinalTrip(brief=brief, destination=dest, verdict=verdict, alternatives=alts,
                     itinerary=itinerary, logistics=logistics, budget=budget, review=review)
    await emit({"type": "final", "trip": json.loads(trip.model_dump_json()),
                "usage": {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens}})
    return trip
