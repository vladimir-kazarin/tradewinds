"""Typed contracts between agents. Every agent ends its run by calling a
`submit` tool whose input schema is one of these models, so outputs are
validated Pydantic objects instead of free text we have to parse.

Invariants that can be checked inside one model (sums, date arithmetic) live
here as validators: a failing submit goes back to the model to fix.
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator

AIRPORT = r"^[A-Z]{3}$"
MAX_NIGHTS = 60


class TripBrief(BaseModel):
    """What the traveler actually wants, extracted from the conversation."""
    start_date: date
    end_date: date
    nights: int = Field(ge=1, le=MAX_NIGHTS)
    origin: str = Field(description="Departure city/airport, e.g. 'Boston (BOS)'")
    budget_usd: int = Field(description="Total trip budget incl. flights, USD")
    vibe: list[str] = Field(description="Mood keywords, e.g. ['chill', 'tropical', 'surf']")
    must_haves: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    travel_style: str = Field(description="e.g. 'slow travel, 2-3 bases max'; note any assumptions here")
    countries_wanted: Literal["one", "several", "either"] = Field(
        "either", description="Whether the traveler wants one country, several, or doesn't mind")

    @model_validator(mode="after")
    def _dates_line_up(self) -> TripBrief:
        if self.end_date <= self.start_date:
            raise ValueError("end_date must be after start_date")
        span = (self.end_date - self.start_date).days
        if self.nights != span:
            raise ValueError(f"nights must equal end_date - start_date ({span}), got {self.nights}")
        return self


class RoughCost(BaseModel):
    """Whole-trip mid-range estimate shown on an option card before it's planned."""
    flights_usd: int = Field(ge=0)
    lodging_usd: int = Field(ge=0, description="All nights combined")
    daily_spend_usd: int = Field(ge=0, description="Food, activities and local transport for the whole stay")
    transfers_usd: int = Field(0, ge=0, description="Travel between regions/countries, 0 if none")
    total_usd: int = Field(ge=0)

    @model_validator(mode="after")
    def _total_is_sum(self) -> RoughCost:
        parts = self.flights_usd + self.lodging_usd + self.daily_spend_usd + self.transfers_usd
        if self.total_usd != parts:
            raise ValueError(f"total_usd must equal the sum of the four parts ({parts}), got {self.total_usd}")
        return self


class DestinationOption(BaseModel):
    name: str = Field(description="Region, town or combination, e.g. 'Nicoya Peninsula'")
    countries: list[str] = Field(min_length=1, description="Countries visited, in order")
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False, description="Map centre")
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)
    why: str = Field(description="Why it fits the vibe, 1-2 sentences")
    weather_summary: str = Field(description="Expected weather for the travel window, from the climate tool")
    est_flight_hours: float
    tradeoffs: str
    rough_cost: RoughCost


class ScoutReport(BaseModel):
    options: list[DestinationOption] = Field(min_length=3, max_length=3)


class OptionScore(BaseModel):
    name: str
    vibe_fit: int = Field(ge=1, le=10)
    weather: int = Field(ge=1, le=10)
    budget_fit: int = Field(ge=1, le=10)
    ease: int = Field(ge=1, le=10)
    total: int


class OptionPitch(BaseModel):
    name: str
    pitch: str = Field(max_length=160, description="Warm one-line pitch shown to the traveler, max ~140 chars")


class VibeVerdict(BaseModel):
    scores: list[OptionScore]
    pick: str = Field(description="Name of the chosen option, must match one option name")
    reasoning: str
    pitches: list[OptionPitch] = Field(default_factory=list, description="One per option")
    ranking: list[str] = Field(default_factory=list, description="All option names, best first")


class DayPlan(BaseModel):
    day: int
    date: date
    base: str = Field(description="Town you sleep in")
    country: str
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
    depart_airport: str = Field(pattern=AIRPORT, description="3-letter IATA code of the home airport")
    arrive_airport: str = Field(pattern=AIRPORT, description="3-letter IATA code where the trip starts")
    return_from_airport: str = Field(pattern=AIRPORT, description="3-letter IATA code flying home; "
                                     "differs from arrive_airport only for open-jaw trips")


class Stay(BaseModel):
    base: str
    country: str
    nights: int
    kind: str = Field(description="e.g. 'surf hostel private room', 'jungle cabina'")
    est_per_night_usd: int


class Transfer(BaseModel):
    from_base: str
    to_base: str
    mode: str = Field(description="e.g. 'flight', 'ferry', 'bus'")
    est_usd: int = Field(ge=0)


class Logistics(BaseModel):
    flights: FlightPlan
    stays: list[Stay]
    transfers: list[Transfer] = Field(default_factory=list, description="Legs between bases in different countries")
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

    @model_validator(mode="after")
    def _total_is_sum(self) -> Budget:
        s = sum(line.usd for line in self.lines)
        if self.total_usd != s:
            raise ValueError(f"total_usd must equal the sum of lines ({s}), got {self.total_usd}")
        return self


class Review(BaseModel):
    approved: bool
    issues: list[str] = Field(default_factory=list, description="Concrete, fixable problems")
    summary: str


class BriefResult(BaseModel):
    """First chat turn: either ask what's missing, or decide."""
    reply: str = Field(description="Friendly one or two sentences shown to the traveler")
    questions: list[str] = Field(default_factory=list, max_length=5,
                                 description="Short questions about what's missing; empty when deciding")
    brief: TripBrief | None = Field(None, description="Set when nothing important is missing")

    @model_validator(mode="after")
    def _exactly_one(self) -> BriefResult:
        if bool(self.questions) == (self.brief is not None):
            raise ValueError("Provide either non-empty questions or a brief, not both and not neither")
        return self


class BriefDecision(BaseModel):
    """Second chat turn: always decide."""
    reply: str = Field(description="Friendly one or two sentences shown to the traveler")
    brief: TripBrief


class RankedOption(BaseModel):
    option: DestinationOption
    pitch: str
    best: bool


class StayLink(BaseModel):
    base: str
    country: str
    url: str


class TripLinks(BaseModel):
    flights: str
    stays: list[StayLink]


class PlanResult(BaseModel):
    option_name: str
    itinerary: Itinerary
    logistics: Logistics
    budget: Budget
    review: Review
    links: TripLinks


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=1000)
