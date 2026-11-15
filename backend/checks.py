"""Deterministic checks that span two agents' outputs.

Pydantic validators can only see one model. These compare the itinerary with
logistics (and the chosen option) and return plain-language issues that go
back to the agent that has to fix them. An empty list means consistent.
"""
from __future__ import annotations

import unicodedata

from schemas import DestinationOption, Itinerary, Logistics


def _norm(s: str) -> str:
    decomposed = unicodedata.normalize("NFKD", s)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold().strip()


def base_sequence(itinerary: Itinerary, nights: int) -> list[tuple[str, str, int]]:
    """Consecutive (base, country, nights) runs over the nights slept; the departure day is excluded."""
    runs: list[tuple[str, str, int]] = []
    for d in sorted(itinerary.days, key=lambda d: d.day):
        if d.day > nights:
            continue
        if runs and _norm(runs[-1][0]) == _norm(d.base) and _norm(runs[-1][1]) == _norm(d.country):
            base, country, n = runs[-1]
            runs[-1] = (base, country, n + 1)
        else:
            runs.append((d.base, d.country, 1))
    return runs


def stay_mismatches(itinerary: Itinerary, logistics: Logistics, nights: int) -> list[str]:
    expected = base_sequence(itinerary, nights)
    stays = [(s.base, s.country, s.nights) for s in logistics.stays]
    issues: list[str] = []
    for i in range(max(len(expected), len(stays))):
        want = expected[i] if i < len(expected) else None
        got = stays[i] if i < len(stays) else None
        if want is None:
            issues.append(f"Stay {i + 1} ({got[0]}, {got[1]}) has no matching base in the itinerary; remove it.")
        elif got is None:
            issues.append(f"Missing a stay for {want[0]}, {want[1]} ({want[2]} nights) from the itinerary.")
        elif (_norm(want[0]), _norm(want[1])) != (_norm(got[0]), _norm(got[1])):
            issues.append(f"Stay {i + 1} is {got[0]}, {got[1]} but the itinerary sleeps in {want[0]}, {want[1]}.")
        elif want[2] != got[2]:
            issues.append(f"Stay in {want[0]} has {got[2]} nights but the itinerary spends {want[2]} nights there.")
    total = sum(s.nights for s in logistics.stays)
    if total != nights:
        issues.append(f"Stay nights add up to {total} but the trip has {nights} nights.")
    return issues


def country_mismatches(option: DestinationOption, itinerary: Itinerary, logistics: Logistics) -> list[str]:
    allowed = {_norm(c) for c in option.countries}
    listed = ", ".join(option.countries)
    issues = [f"Day {d.day} ({d.base}) is in {d.country}, which isn't one of this trip's countries ({listed})."
              for d in itinerary.days if _norm(d.country) not in allowed]
    issues += [f"Stay in {s.base} is in {s.country}, which isn't one of this trip's countries ({listed})."
               for s in logistics.stays if _norm(s.country) not in allowed]
    return issues
