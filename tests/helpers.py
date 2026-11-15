"""Builders for itinerary/logistics fixtures used across tests."""
from __future__ import annotations

from datetime import date, timedelta

from schemas import DayPlan, FlightPlan, Itinerary, Logistics, Stay


def itinerary(runs: list[tuple[str, str, int]], start: date = date(2026, 12, 1)) -> Itinerary:
    """runs = [(base, country, nights)]; adds a departure day in the last base."""
    days, n = [], 1
    for base, country, nights in runs:
        for _ in range(nights):
            days.append(_day(n, start, base, country))
            n += 1
    days.append(_day(n, start, runs[-1][0], runs[-1][1]))
    return Itinerary(headline="h", days=days, highlights=[])


def _day(n: int, start: date, base: str, country: str) -> DayPlan:
    return DayPlan(day=n, date=start + timedelta(days=n - 1), base=base, country=country, lat=0, lon=0,
                   title="t", morning="m", afternoon="a", evening="e", pace="easy")


def logistics(stays: list[tuple[str, str, int]], dep="BOS", arr="LIR", ret="LIR") -> Logistics:
    return Logistics(
        flights=FlightPlan(route="r", est_round_trip_usd=500, notes="", depart_airport=dep,
                           arrive_airport=arr, return_from_airport=ret),
        stays=[Stay(base=b, country=c, nights=n, kind="k", est_per_night_usd=80) for b, c, n in stays],
        getting_around="shuttles", packing=[])
