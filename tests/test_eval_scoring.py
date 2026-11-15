from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))

from helpers import itinerary, logistics  # noqa: E402
from schemas import Budget, BudgetLine, PlanResult, Review, TripLinks  # noqa: E402
from scoring import score_options, score_plan  # noqa: E402

EXPECT = {"budget_usd": 1500, "nights": 14, "origin": "BOS|Boston", "min_countries": 2, "must": ["surf"],
          "avoid": ["party hostel"]}


def _options_event(brief, option_cr, totals=(1200, 1300, 1400), countries=(["Costa Rica"], ["Costa Rica", "Panama"], ["Peru"])):
    opts = []
    for t, c in zip(totals, countries):
        o = option_cr.model_dump(mode="json")
        o["countries"] = c
        o["rough_cost"] = {**o["rough_cost"], "total_usd": t}
        opts.append({"option": o, "pitch": "surf town", "best": False})
    return {"type": "options", "reply": "", "brief": {**brief.model_dump(mode="json"), "budget_usd": 1500}, "options": opts}


def test_good_options_pass_every_check(brief, option_cr):
    checks = score_options(EXPECT, _options_event(brief, option_cr))
    assert all(checks.values()), checks


def test_over_budget_option_and_wrong_nights_fail(brief, option_cr):
    ev = _options_event(brief, option_cr, totals=(1200, 1600, 1400))
    ev["brief"]["nights"] = 10
    checks = score_options(EXPECT, ev)
    assert not checks["options_within_budget"] and not checks["brief_nights"] and checks["brief_origin"]


def _plan(runs, total=1300, approved=True, tips=("cook",), text_extra=""):
    it = itinerary(runs)
    if text_extra:
        it = it.model_copy(update={"headline": text_extra})
    return PlanResult(option_name="x", itinerary=it, logistics=logistics(runs),
                      budget=Budget(lines=[BudgetLine(category="all", usd=total)], total_usd=total,
                                    within_budget=total <= 1500, notes="", saving_tips=list(tips)),
                      review=Review(approved=approved, summary=""), links=TripLinks(flights="https://x", stays=[]))


def test_plan_checks(brief):
    b = brief.model_copy(update={"budget_usd": 1500})
    runs = [("Nosara", "Costa Rica", 14)]
    assert all(score_plan(EXPECT, b, _plan(runs)).values())
    bad = score_plan(EXPECT, b, _plan(runs, total=1700, approved=False, tips=(), text_extra="Party hostel crawl"))
    assert not bad["plan_within_budget"] and not bad["reviewer_approved"]
    assert not bad["saving_tips"] and not bad["avoid_respected"] and bad["stays_match_itinerary"]


def test_must_haves_are_searched_in_options_not_the_brief(brief, option_cr):
    ev = _options_event(brief, option_cr)
    for o in ev["options"]:
        o["pitch"] = "quiet beaches"
        o["option"]["why"] = "quiet beaches"
    ev["brief"]["must_haves"] = ["surf lessons"]
    assert not score_options(EXPECT, ev)["must_haves_in_options"]
