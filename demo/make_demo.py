"""Record a demo run by driving the REAL orchestrator with a scripted fake model.

This doubles as an integration test of the agent loop: tool calls, parallel agents,
validation, and the reviewer->revision loop all execute for real; only the LLM and
HTTP calls are stubbed. Run:  python demo/make_demo.py
"""
from __future__ import annotations

import asyncio
import itertools
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import agent as agent_mod  # noqa: E402
import crew  # noqa: E402

PROMPT = ("I've got two weeks starting Dec 1 and I'm honestly burnt out. I want somewhere chill and tropical, "
          "warm water, maybe finally learn to surf properly, great food, NOT a party scene. "
          "Flying from Boston, around $3k all in.")

_ids = itertools.count(1)


def tool_use(name, inp):
    return NS(type="tool_use", id=f"tu_{next(_ids)}", name=name, input=inp)


def text(t):
    return NS(type="text", text=t)


def resp(*blocks):
    return NS(content=list(blocks), usage=NS(input_tokens=1800, output_tokens=650))


# ---------------------------------------------------------------- scripted outputs
BRIEF = dict(start_date="2026-12-01", end_date="2026-12-15", nights=14, origin="Boston (BOS)", budget_usd=3000,
             vibe=["chill", "tropical", "warm water", "learn to surf", "great food", "quiet"],
             must_haves=["beginner-friendly surf lessons", "good local food"], avoid=["party scene"],
             travel_style="Slow travel, 2-3 bases max; mid-range comfort; assumed solo traveler")

OPTS = [
    dict(name="Nicoya Peninsula", country="Costa Rica", lat=9.9781, lon=-85.6528,
         why="Laid-back surf towns (Nosara, Santa Teresa, Sámara) with gentle beginner breaks, yoga culture and "
             "great sodas. Dry season just starting.",
         weather_summary="Avg high 31°C / low 23°C, 2 of 15 days with rain, ~8 h sunshine/day",
         est_flight_hours=6.5, tradeoffs="Dirt roads between towns; Santa Teresa pricier than it used to be."),
    dict(name="Bocas del Toro", country="Panama", lat=9.3403, lon=-82.2420,
         why="Caribbean islands on stilts, calm bays, cheap and very slow-paced.",
         weather_summary="Avg high 29°C / low 24°C, 8 of 15 days with rain, ~5 h sunshine/day",
         est_flight_hours=9.0, tradeoffs="December is wet on the Caribbean side; Bocas Town has a party strip."),
    dict(name="Siargao", country="Philippines", lat=9.8482, lon=126.0458,
         why="Palm-jungle island with a soft surf scene and incredible island-hopping.",
         weather_summary="Avg high 29°C / low 24°C, 11 of 15 days with rain, ~4 h sunshine/day",
         est_flight_hours=22.0, tradeoffs="Peak rainy season in December and ~2 days lost to travel each way."),
]

VERDICT = dict(scores=[
    dict(name="Nicoya Peninsula", vibe_fit=9, weather=9, budget_fit=8, ease=8, total=34),
    dict(name="Bocas del Toro", vibe_fit=8, weather=6, budget_fit=9, ease=7, total=30),
    dict(name="Siargao", vibe_fit=10, weather=5, budget_fit=6, ease=4, total=25),
], pick="Nicoya Peninsula", reasoning=(
    "Nicoya wins on what matters most for a burnt-out traveler in December: dry-season sun, beginner surf, and a "
    "6.5 h flight. Siargao is the dreamiest vibe but rain and a 22 h journey would eat the trip; Bocas is cheapest "
    "but wet this time of year."))

NOS, ST, SAM = (9.9781, -85.6528), (9.6464, -85.1686), (9.8800, -85.5280)


def day(n, d, base, ll, title, m, a, e, pace):
    return dict(day=n, date=f"2026-12-{d:02d}", base=base, lat=ll[0], lon=ll[1], title=title,
                morning=m, afternoon=a, evening=e, pace=pace)


DAYS_V1 = [
    day(1, 1, "Nosara", NOS, "Touch down, slow down", "Land in Liberia (LIR) around midday",
        "2.5 h shuttle to Nosara, check in near Playa Guiones", "Sunset walk on Guiones, casado at a local soda", "slow"),
    day(2, 2, "Nosara", NOS, "First wave", "Beginner surf lesson on the soft Guiones inside",
        "Nap, smoothie bowl, hammock", "Sunset yoga class", "easy"),
    day(3, 3, "Nosara", NOS, "Into the green", "Second surf lesson, practice pop-ups",
        "Ostional wildlife refuge, nesting turtles if lucky", "Dinner at a farm-to-table spot in Pelada", "easy"),
    day(4, 4, "Nosara", NOS, "Nothing day", "Sleep in, coffee, read on the beach",
        "Surf on your own at mid-tide", "Tide pools at Playa Pelada at sunset", "slow"),
    day(5, 5, "Santa Teresa", ST, "Down the coast", "Early surf lesson then check out",
        "Shuttle down the coast to Santa Teresa", "Fish tacos and sunset at Playa Carmen", "active"),
    day(6, 6, "Santa Teresa", ST, "Settle in", "Surf Playa Carmen's beginner peaks",
        "Explore town by bike", "Poke bowl and live acoustic music", "easy"),
    day(7, 7, "Santa Teresa", ST, "Montezuma falls", "Yoga", "Day trip to Montezuma waterfall and village",
        "Back for sunset at Playa Hermosa", "active"),
    day(8, 8, "Santa Teresa", ST, "Island day", "Boat trip to Isla Tortuga, snorkeling",
        "White-sand lunch on the island", "Early night", "easy"),
    day(9, 9, "Santa Teresa", ST, "Rest", "Surf at dawn when it's glassy", "Spa / massage",
        "Cook dinner with market produce", "slow"),
    day(10, 10, "Santa Teresa", ST, "Mal País", "Surf lesson #4, try a green wave",
        "Walk to the Mal País fishing village", "Fresh ceviche at the docks", "easy"),
    day(11, 11, "Sámara", SAM, "The calm bay", "Morning yoga, check out",
        "Shuttle to Sámara", "Barefoot dinner on the beach", "easy"),
    day(12, 12, "Sámara", SAM, "Glass-flat bay", "Kayak to Isla Chora", "Hammock + book",
        "Sunset from the Mirador", "slow"),
    day(13, 13, "Sámara", SAM, "Last surf", "Final surf session on Sámara's gentle beach break",
        "Cooking class: Costa Rican classics", "Farewell dinner", "easy"),
    day(14, 14, "Sámara", SAM, "Soak it in", "Swim, coffee, nothing scheduled",
        "Souvenirs: local coffee and hot sauce", "Last sunset on Playa Carrillo", "slow"),
    day(15, 15, "Sámara", SAM, "Pura vida, home", "Slow breakfast", "2 h shuttle to LIR, afternoon flight home",
        "Land in Boston", "slow"),
]
DAYS_V2 = [d if d["day"] != 5 else day(
    5, 5, "Santa Teresa", ST, "Coast-road travel day", "Lazy breakfast, last swim at Guiones",
    "Private 4x4 shuttle down the coast road (~3 h, unpaved); no lessons today",
    "Arrive, fish tacos and sunset at Playa Carmen", "slow") for d in DAYS_V1]

ITIN = lambda days: dict(headline="Two slow weeks of sun, gentle waves and gallo pinto on Costa Rica's Nicoya coast",
                          days=days, highlights=["5 surf lessons from first pop-up to green waves",
                                                 "Turtle nesting at Ostional", "Snorkeling at Isla Tortuga",
                                                 "Kayaking to Isla Chora", "Zero party towns"])

LOG = dict(flights=dict(route="BOS → LIR (1 stop via MIA/ATL) round trip", est_round_trip_usd=520,
                        notes="Estimate for early December; book ~6 weeks out. LIR is much closer than SJO."),
           stays=[dict(base="Nosara", nights=4, kind="Jungle cabina 5 min from Guiones", est_per_night_usd=85),
                  dict(base="Santa Teresa", nights=6, kind="Surf-hostel private room with pool", est_per_night_usd=95),
                  dict(base="Sámara", nights=4, kind="Beachfront guesthouse", est_per_night_usd=70)],
           getting_around="Shared and private shuttles between towns (no car needed); bikes and walking within each town.",
           packing=["Reef-safe sunscreen", "Rash guard", "Light rain shell", "Headlamp", "Dry bag",
                    "Sandals + one pair of trainers", "Insect repellent", "USD cash for shuttles"])

BUDGET = dict(lines=[dict(category="Flights", usd=520), dict(category="Lodging (14 nights)", usd=1190),
                     dict(category="Shuttles", usd=160), dict(category="Food (~$40/day)", usd=560),
                     dict(category="Surf lessons, yoga, tours", usd=300), dict(category="10% buffer", usd=270)],
              total_usd=3000, within_budget=True,
              notes="Right at budget. Easiest cuts: cook 3 dinners at the hostel (-$60), group shuttles (-$40).")

REVIEW_1 = dict(approved=False, issues=[
    "Day 5 is marked 'active' with an early surf lesson, but Nosara → Santa Teresa is ~3 h on unpaved coast road. "
    "Make it a slow travel day and use a private 4x4 shuttle."],
    summary="Strong plan; one unrealistic transfer day.")
REVIEW_2 = dict(approved=True, issues=[], summary=(
    "Dates, 14 nights and 3 bases line up; budget lands at $3,000; surf lessons and food are covered; no party towns. "
    "Day 5 is now a realistic travel day."))

SCRIPTS = {
    "brief": [[text("Burnt out + tropical + learn to surf: I'll assume solo, mid-range."), tool_use("submit", BRIEF)]],
    "scout": [
        [text("Thinking Pacific Costa Rica, Caribbean Panama and Siargao. Checking each one."),
         tool_use("geocode", {"place": "Nosara"}), tool_use("geocode", {"place": "Bocas del Toro"}),
         tool_use("geocode", {"place": "Siargao"})],
        [tool_use("climate_for_window", {"lat": 9.9781, "lon": -85.6528, "start_date": "2026-12-01", "end_date": "2026-12-15"}),
         tool_use("climate_for_window", {"lat": 9.3403, "lon": -82.2420, "start_date": "2026-12-01", "end_date": "2026-12-15"}),
         tool_use("climate_for_window", {"lat": 9.8482, "lon": 126.0458, "start_date": "2026-12-01", "end_date": "2026-12-15"})],
        [text("December is dry season in Nicoya but wet in Bocas and Siargao. That changes things."),
         tool_use("submit", {"options": OPTS})],
    ],
    "judge": [[tool_use("submit", VERDICT)]],
    "itinerary": [
        [text("Three bases running down the coast, ending close to LIR."),
         tool_use("geocode", {"place": "Nosara"}), tool_use("geocode", {"place": "Santa Teresa, Costa Rica"}),
         tool_use("geocode", {"place": "Sámara"})],
        [tool_use("ocean_conditions", {"lat": 9.95, "lon": -85.68}),
         tool_use("ocean_conditions", {"lat": 9.64, "lon": -85.19}),
         tool_use("ocean_conditions", {"lat": 9.87, "lon": -85.55})],
        [text("Waves are small and clean, good for beginners."), tool_use("submit", ITIN(DAYS_V1))],
        # revision round
        [text("Good catch, turning day 5 into a slow travel day."), tool_use("submit", ITIN(DAYS_V2))],
    ],
    "logistics": [[tool_use("submit", LOG)]],
    "budget": [[tool_use("submit", BUDGET)], [tool_use("submit", BUDGET)]],
    "reviewer": [[tool_use("submit", REVIEW_1)], [tool_use("submit", REVIEW_2)]],
}

TOOL_RESULTS = {
    ("geocode", "Nosara"): {"matches": [{"name": "Nosara", "country": "Costa Rica", "admin": "Guanacaste", "lat": 9.9781, "lon": -85.6528}]},
    ("geocode", "Bocas del Toro"): {"matches": [{"name": "Bocas del Toro", "country": "Panama", "admin": "Bocas del Toro", "lat": 9.3403, "lon": -82.242}]},
    ("geocode", "Siargao"): {"matches": [{"name": "General Luna", "country": "Philippines", "admin": "Caraga", "lat": 9.8482, "lon": 126.0458}]},
    ("geocode", "Santa Teresa, Costa Rica"): {"matches": [{"name": "Santa Teresa", "country": "Costa Rica", "admin": "Puntarenas", "lat": 9.6464, "lon": -85.1686}]},
    ("geocode", "Sámara"): {"matches": [{"name": "Sámara", "country": "Costa Rica", "admin": "Guanacaste", "lat": 9.88, "lon": -85.528}]},
    ("climate_for_window", 9.9781): {"proxy_window": "2025-12-01 to 2025-12-15", "avg_high_c": 31.2, "avg_low_c": 23.1, "rainy_days": 2, "total_days": 15, "avg_sunshine_hours": 8.1},
    ("climate_for_window", 9.3403): {"proxy_window": "2025-12-01 to 2025-12-15", "avg_high_c": 29.0, "avg_low_c": 24.2, "rainy_days": 8, "total_days": 15, "avg_sunshine_hours": 5.2},
    ("climate_for_window", 9.8482): {"proxy_window": "2025-12-01 to 2025-12-15", "avg_high_c": 28.8, "avg_low_c": 24.4, "rainy_days": 11, "total_days": 15, "avg_sunshine_hours": 4.3},
    ("ocean_conditions", 9.95): {"avg_max_wave_m": 1.1, "avg_period_s": 11.8, "note": "Live 7-day outlook, not a seasonal average."},
    ("ocean_conditions", 9.64): {"avg_max_wave_m": 1.3, "avg_period_s": 12.4, "note": "Live 7-day outlook, not a seasonal average."},
    ("ocean_conditions", 9.87): {"avg_max_wave_m": 0.8, "avg_period_s": 11.1, "note": "Live 7-day outlook, not a seasonal average."},
}


class FakeMessages:
    def __init__(self):
        self.cursors = {k: iter(v) for k, v in SCRIPTS.items()}

    async def create(self, *, system, **_):
        name = next(a.name for a in crew.CREW if a.system == system)
        await asyncio.sleep({"brief": 1.2, "judge": 1.5, "itinerary": 2.2, "logistics": 2.6}.get(name, 1.4))
        return resp(*next(self.cursors[name]))


async def fake_run_tool(name, args):
    await asyncio.sleep(0.5)
    key = (name, args.get("place") if name == "geocode" else args["lat"])
    return TOOL_RESULTS[key]


async def main():
    agent_mod._client = NS(messages=FakeMessages())
    agent_mod.run_tool = fake_run_tool
    out = (ROOT / "demo" / "sample_run.jsonl").open("w")
    last = time.monotonic()

    async def emit(ev):
        nonlocal last
        now = time.monotonic()
        out.write(json.dumps({**ev, "_delay": round(now - last, 2)}, default=str) + "\n")
        last = now

    trip = await crew.plan_trip(PROMPT, emit)
    out.close()
    assert trip.review.approved and trip.itinerary.days[4].pace == "slow"
    assert sum(s.nights for s in trip.logistics.stays) == trip.brief.nights
    assert sum(l.usd for l in trip.budget.lines) == trip.budget.total_usd
    print("demo recorded:", trip.destination.name, len(trip.itinerary.days), "days")


if __name__ == "__main__":
    asyncio.run(main())
