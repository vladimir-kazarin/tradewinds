from __future__ import annotations

from checks import base_sequence, country_mismatches, stay_mismatches
from helpers import itinerary, logistics

CR = "Costa Rica"
RUNS = [("Nosara", CR, 4), ("Santa Teresa", CR, 6), ("Sámara", CR, 4)]


def test_base_sequence_groups_consecutive_days():
    it = itinerary(RUNS)
    assert len(it.days) == 15
    assert base_sequence(it, 14) == RUNS


def test_matching_stays_have_no_issues():
    assert stay_mismatches(itinerary(RUNS), logistics(RUNS), 14) == []


def test_accent_and_case_insensitive_match():
    stays = [("nosara", "costa rica", 4), ("SANTA TERESA", CR, 6), ("Samara", CR, 4)]
    assert stay_mismatches(itinerary(RUNS), logistics(stays), 14) == []


def test_wrong_nights_reported():
    stays = [("Nosara", CR, 5), ("Santa Teresa", CR, 5), ("Sámara", CR, 4)]
    issues = stay_mismatches(itinerary(RUNS), logistics(stays), 14)
    assert issues
    assert any("Nosara" in i and "5" in i and "4" in i for i in issues)


def test_extra_stay_reported():
    stays = RUNS + [("Liberia", CR, 1)]
    issues = stay_mismatches(itinerary(RUNS), logistics(stays), 14)
    assert any("Liberia" in i for i in issues)
    assert any("15" in i for i in issues)  # nights total


def test_country_outside_option_reported(option_cr):
    runs = [("Nosara", CR, 7), ("Bocas Town", "Panama", 7)]
    issues = country_mismatches(option_cr, itinerary(runs), logistics(runs))
    day_issues = [i for i in issues if "Day 8" in i]
    assert len(day_issues) == 1 and "Panama" in day_issues[0]
    assert any("Bocas Town" in i and "stay" in i.lower() for i in issues)
