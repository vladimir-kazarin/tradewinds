"""Shared pytest setup: backend modules use flat imports, so put backend/ on sys.path."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from schemas import DestinationOption, RoughCost, TripBrief  # noqa: E402


@pytest.fixture
def brief() -> TripBrief:
    return TripBrief(start_date="2026-12-01", end_date="2026-12-15", nights=14, origin="Boston (BOS)",
                     budget_usd=3000, vibe=["chill"], travel_style="slow travel")


@pytest.fixture
def option_cr() -> DestinationOption:
    return DestinationOption(
        name="Nicoya Peninsula", countries=["Costa Rica"], lat=9.98, lon=-85.65, why="Gentle surf towns.",
        weather_summary="Dry and sunny", est_flight_hours=6.5, tradeoffs="Dirt roads",
        rough_cost=RoughCost(flights_usd=520, lodging_usd=1190, daily_spend_usd=860, total_usd=2570))
