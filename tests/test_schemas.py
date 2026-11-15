from __future__ import annotations

import pytest
from pydantic import ValidationError

from schemas import (BriefResult, Budget, ChatTurn, FlightPlan, RoughCost, ScoutReport, TripBrief)

BRIEF = dict(start_date="2026-12-01", end_date="2026-12-15", nights=14, origin="Boston (BOS)",
             budget_usd=3000, vibe=["chill"], travel_style="slow")


def test_brief_nights_must_match_dates():
    TripBrief(**BRIEF)
    with pytest.raises(ValidationError):
        TripBrief(**{**BRIEF, "nights": 13})
    with pytest.raises(ValidationError):
        TripBrief(**{**BRIEF, "start_date": "2026-12-16"})


def test_rough_cost_total_must_equal_parts():
    RoughCost(flights_usd=520, lodging_usd=1190, daily_spend_usd=860, transfers_usd=100, total_usd=2670)
    with pytest.raises(ValidationError):
        RoughCost(flights_usd=520, lodging_usd=1190, daily_spend_usd=860, transfers_usd=100, total_usd=2600)


def test_scout_requires_exactly_three_options(option_cr):
    ScoutReport(options=[option_cr] * 3)
    for n in (2, 4):
        with pytest.raises(ValidationError):
            ScoutReport(options=[option_cr] * n)


def test_airport_codes_validated():
    base = dict(route="BOS-LIR", est_round_trip_usd=500, notes="", depart_airport="BOS",
                arrive_airport="LIR", return_from_airport="LIR")
    FlightPlan(**base)
    for bad in ("bos", "BOST"):
        with pytest.raises(ValidationError):
            FlightPlan(**{**base, "depart_airport": bad})


def test_budget_total_must_equal_lines():
    lines = [dict(category="a", usd=100), dict(category="b", usd=50)]
    Budget(lines=lines, total_usd=150, within_budget=True, notes="")
    with pytest.raises(ValidationError):
        Budget(lines=lines, total_usd=140, within_budget=True, notes="")


def test_brief_result_exactly_one_of_questions_or_brief():
    BriefResult(reply="hi", questions=["When?"])
    ALL = ["origin", "dates", "budget"]
    BriefResult(reply="hi", brief=BRIEF, basics_stated=ALL)
    with pytest.raises(ValidationError):
        BriefResult(reply="hi", questions=["When?"], brief=BRIEF, basics_stated=ALL)
    with pytest.raises(ValidationError):
        BriefResult(reply="hi")


def test_chat_turn_limits():
    ChatTurn(role="user", text="x" * 1000)
    with pytest.raises(ValidationError):
        ChatTurn(role="user", text="x" * 1001)
    with pytest.raises(ValidationError):
        ChatTurn(role="user", text="")


def test_extra_saving_tips_are_trimmed_not_rejected():
    b = Budget(lines=[dict(category="a", usd=1)], total_usd=1, within_budget=True, notes="",
               saving_tips=[f"tip {i}" for i in range(8)])
    assert len(b.saving_tips) == 5


def test_brief_cannot_decide_without_the_stated_basics():
    with pytest.raises(ValidationError):
        BriefResult(reply="ok", brief=BRIEF, basics_stated=["budget"])
    BriefResult(reply="ok", brief=BRIEF, basics_stated=["origin", "dates", "budget"])
    BriefResult(reply="q", questions=["Where from?"], basics_stated=[])
