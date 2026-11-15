"""Deterministic checks for one eval case. Pure functions: no model calls, no I/O.

Each check is a named bool, so runs can be compared check by check.
"""
from __future__ import annotations

import json
import re
from typing import Any

from checks import itinerary_mismatches, stay_mismatches
from schemas import PlanResult, TripBrief

BUDGET_TOLERANCE = 0.10  # the brief may round a converted currency
NIGHTS_TOLERANCE = 1


def _found(pattern: str, text: str) -> bool:
    return re.search(pattern, text, re.IGNORECASE) is not None


def score_options(expect: dict[str, Any], ev: dict[str, Any]) -> dict[str, bool]:
    """Checks on step 1's terminal `options` event."""
    brief = ev["brief"]
    options = [o["option"] for o in ev["options"]]
    checks = {
        "three_options": len(options) == 3,
        "options_within_budget": all(o["rough_cost"]["total_usd"] <= brief["budget_usd"] for o in options),
        "brief_budget": abs(brief["budget_usd"] - expect["budget_usd"]) <= expect["budget_usd"] * BUDGET_TOLERANCE,
        "brief_nights": abs(brief["nights"] - expect["nights"]) <= NIGHTS_TOLERANCE,
        "brief_origin": _found(expect["origin"], brief["origin"]),
    }
    if "min_countries" in expect:
        checks["multi_country_option"] = any(len(o["countries"]) >= expect["min_countries"] for o in options)
    if "must" in expect:
        text = json.dumps(ev, ensure_ascii=False)
        checks["must_haves_in_options"] = all(_found(p, text) for p in expect["must"])
    return checks


def score_plan(expect: dict[str, Any], brief: TripBrief, plan: PlanResult) -> dict[str, bool]:
    """Checks on step 2's PlanResult for the best-value option."""
    itinerary_text = json.dumps(plan.itinerary.model_dump(mode="json"), ensure_ascii=False).lower()
    checks = {
        "plan_within_budget": plan.budget.total_usd <= brief.budget_usd and plan.budget.within_budget,
        "stays_match_itinerary": stay_mismatches(plan.itinerary, plan.logistics, brief.nights) == [],
        "days_and_dates": itinerary_mismatches(brief, plan.itinerary) == [],
        "reviewer_approved": plan.review.approved,
        "saving_tips": len(plan.budget.saving_tips) > 0,
    }
    if "avoid" in expect:
        checks["avoid_respected"] = not any(a.lower() in itinerary_text for a in expect["avoid"])
    return checks
