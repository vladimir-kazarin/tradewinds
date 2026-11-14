"""Typed contracts between agents. Every agent ends its run by calling a
`submit_*` tool whose input schema is one of these models, so outputs are
validated Pydantic objects instead of free text we have to parse."""
from __future__ import annotations

from datetime import date
from pydantic import BaseModel, Field


class TripBrief(BaseModel):
    """What the traveler actually wants, extracted from a free-form prompt."""
    start_date: date
    end_date: date
    nights: int
    origin: str = Field(description="Departure city/airport, e.g. 'Boston (BOS)'")
    budget_usd: int = Field(description="Total trip budget incl. flights, USD")
    vibe: list[str] = Field(description="Mood keywords, e.g. ['chill', 'tropical', 'surf']")
    must_haves: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    travel_style: str = Field(description="e.g. 'slow travel, 2-3 bases max'")


class DestinationOption(BaseModel):
    name: str = Field(description="Region or town, e.g. 'Nicoya Peninsula'")
    country: str
    lat: float
    lon: float
    why: str = Field(description="Why it fits the vibe, 1-2 sentences")
    weather_summary: str = Field(description="Expected weather for the travel window, from the climate tool")
    est_flight_hours: float
    tradeoffs: str


class ScoutReport(BaseModel):
    options: list[DestinationOption] = Field(min_length=2, max_length=4)


class OptionScore(BaseModel):
    name: str
    vibe_fit: int = Field(ge=1, le=10)
    weather: int = Field(ge=1, le=10)
    budget_fit: int = Field(ge=1, le=10)
    ease: int = Field(ge=1, le=10)
    total: int


class VibeVerdict(BaseModel):
    scores: list[OptionScore]
    pick: str = Field(description="Name of the chosen option, must match one option name")
    reasoning: str


class DayPlan(BaseModel):
    day: int
    date: date
    base: str = Field(description="Town you sleep in")
    lat: float
    lon: float
    title: str = Field(description="Short evocative title for the day")
    morning: str
    afternoon: str
    evening: str
    pace: str = Field(description="'slow', 'easy' or 'active'")


class Itinerary(BaseModel):
    headline: str = Field(description="One-line pitch for the trip")
    days: list[DayPlan]
    highlights: list[str] = Field(max_length=5)


class FlightPlan(BaseModel):
    route: str
    est_round_trip_usd: int
    notes: str


class Stay(BaseModel):
    base: str
    nights: int
    kind: str = Field(description="e.g. 'surf hostel private room', 'jungle cabina'")
    est_per_night_usd: int


class Logistics(BaseModel):
    flights: FlightPlan
    stays: list[Stay]
    getting_around: str
    packing: list[str] = Field(max_length=8)


class BudgetLine(BaseModel):
    category: str
    usd: int


class Budget(BaseModel):
    lines: list[BudgetLine]
    total_usd: int
    within_budget: bool
    notes: str


class Review(BaseModel):
    approved: bool
    issues: list[str] = Field(default_factory=list, description="Concrete, fixable problems")
    summary: str


class FinalTrip(BaseModel):
    brief: TripBrief
    destination: DestinationOption
    verdict: VibeVerdict
    alternatives: list[DestinationOption]
    itinerary: Itinerary
    logistics: Logistics
    budget: Budget
    review: Review
