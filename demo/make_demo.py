"""Record a demo run by driving the REAL orchestrator with a scripted fake model.

This doubles as an integration test of both steps: the question round, options,
tool calls, validation, cross-agent checks and the reviewer->revision loop all execute for real; only the LLM and
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
import pipeline  # noqa: E402
from checks import stay_mismatches  # noqa: E402
from schemas import ChatTurn, DestinationOption, TripBrief  # noqa: E402

PROMPT = "Somewhere warm and cheap in December, I'm burnt out. Want to learn to surf, great food, no party hostels."
ANSWER = "Dec 1–15, from Boston, about $1,500 all in. Dorms are fine. One country or two."

_ids = itertools.count(1)


def tool_use(name, inp):
    return NS(type="tool_use", id=f"tu_{next(_ids)}", name=name, input=inp)


def text(t):
    return NS(type="text", text=t)


def resp(*blocks):
    return NS(content=list(blocks), usage=NS(input_tokens=1800, output_tokens=650))


# ---------------------------------------------------------------- scripted outputs
QUESTIONS = dict(reply="A cheap, sunny reset sounds perfect. Two quick things so I can start looking:",
                 questions=["Roughly when, for how long, and where would you fly from?",
                            "What's your total budget, flights included?"])

BRIEF = dict(start_date="2026-12-01", end_date="2026-12-15", nights=14, origin="Boston (BOS)", budget_usd=1500,
             vibe=["chill", "tropical", "warm water", "learn to surf", "great food", "quiet"],
             must_haves=["beginner-friendly surf lessons", "good local food"], avoid=["party scene"],
             travel_style="Backpacker, slow travel, 2-3 bases max; hostel dorms; local buses and shared shuttles; solo",
             countries_wanted="either")
DECISION = dict(reply="Two slow, sunny weeks from Boston on about $1,500, with surf lessons and great cheap food.",
                brief=BRIEF, basics_stated=["origin", "dates", "budget"], followups=["Dorms for every night, or a private room now and then?",
                                        "Anything you'd love to see or do while you're there?"])
NOTE = "A private room for the last two nights would be lovely if it fits."


def cost(flights, lodging, daily, transfers=0):
    return dict(flights_usd=flights, lodging_usd=lodging, daily_spend_usd=daily, transfers_usd=transfers,
                total_usd=flights + lodging + daily + transfers)


OPTS = [
    dict(name="Nicoya Peninsula", countries=["Costa Rica"], lat=9.9781, lon=-85.6528,
         why="Laid-back surf towns (Nosara, Santa Teresa, Sámara) with gentle beginner breaks, yoga culture and "
             "great sodas. Dry season just starting.",
         weather_summary="Avg high 31°C / low 23°C, 2 of 15 days with rain, ~8 h sunshine/day",
         est_flight_hours=6.5, tradeoffs="Dirt roads between towns; Santa Teresa pricier than it used to be.",
         rough_cost=cost(420, 330, 560)),
    dict(name="Caribbean Costa Rica + Bocas del Toro", countries=["Costa Rica", "Panama"], lat=9.3403, lon=-82.2420,
         why="Reggae-slow Puerto Viejo, then a short border hop to Panama's stilt-house islands and calm bays.",
         weather_summary="Avg high 29°C / low 24°C, 8 of 15 days with rain, ~5 h sunshine/day",
         est_flight_hours=7.5, tradeoffs="December is wet on the Caribbean side; Bocas Town has a party strip.",
         rough_cost=cost(480, 280, 470, 45)),
    dict(name="Siargao", countries=["Philippines"], lat=9.8482, lon=126.0458,
         why="Palm-jungle island with a soft surf scene and incredible island-hopping.",
         weather_summary="Avg high 29°C / low 24°C, 11 of 15 days with rain, ~4 h sunshine/day",
         est_flight_hours=22.0, tradeoffs="Peak rainy season in December and ~2 days lost to travel each way.",
         rough_cost=cost(1050, 170, 270)),
]
NICOYA, CARIB, SIARGAO = (o["name"] for o in OPTS)

VERDICT = dict(scores=[
    dict(name=NICOYA, vibe_fit=9, weather=9, budget_fit=8, ease=8, total=34),
    dict(name=CARIB, vibe_fit=8, weather=6, budget_fit=9, ease=7, total=30),
    dict(name=SIARGAO, vibe_fit=10, weather=5, budget_fit=6, ease=4, total=25),
], pick=NICOYA, reasoning=(
    "Nicoya wins on what matters most for a burnt-out traveler in December: dry-season sun, beginner surf, and a "
    "6.5 h flight. Siargao is the dreamiest vibe but rain and a 22 h journey would eat the trip; the Caribbean "
    "combo is cheapest but wet this time of year."),
    pitches=[dict(name=NICOYA, pitch="Dry-season sun, $20 surf-hostel dorms and three sleepy surf towns linked by local buses."),
             dict(name=CARIB, pitch="The cheapest of the three: two countries, $10 rice-and-beans and island hammocks."),
             dict(name=SIARGAO, pitch="Dirt-cheap beds and food once you're there, but the long flight eats most of the budget.")],
    ranking=[NICOYA, CARIB, SIARGAO])

NOS, ST, SAM = (9.9781, -85.6528), (9.6464, -85.1686), (9.8800, -85.5280)


def day(n, d, base, ll, title, m, a, e, pace):
    return dict(day=n, date=f"2026-12-{d:02d}", base=base, country="Costa Rica", lat=ll[0], lon=ll[1], title=title,
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
    "Shared shuttle down the coast road (~3 h, unpaved, $35); no lessons today",
    "Arrive, fish tacos and sunset at Playa Carmen", "slow") for d in DAYS_V1]

ITIN = lambda days: dict(headline="Two slow weeks of sun, gentle waves and gallo pinto on Costa Rica's Nicoya coast",
                          days=days, highlights=["5 surf lessons from first pop-up to green waves",
                                                 "Turtle nesting at Ostional", "Snorkeling at Isla Tortuga",
                                                 "Kayaking to Isla Chora", "Zero party towns"])

LOG = dict(flights=dict(route="BOS → LIR (1 stop via FLL on a budget carrier) round trip", est_round_trip_usd=420,
                        depart_airport="BOS", arrive_airport="LIR", return_from_airport="LIR",
                        notes="Estimate for early December; book ~6 weeks out, carry-on only. LIR is closer and cheaper than SJO."),
           stays=[dict(base="Nosara", country="Costa Rica", nights=4, kind="Surf-hostel dorm 5 min from Guiones", est_per_night_usd=22),
                  dict(base="Santa Teresa", country="Costa Rica", nights=6, kind="Hostel dorm with shared kitchen", est_per_night_usd=26),
                  dict(base="Sámara", country="Costa Rica", nights=4, kind="Family-run guesthouse dorm", est_per_night_usd=18)],
           getting_around="Local buses and one shared shuttle between towns (no car needed); walking and $5/day bike rentals in town.",
           packing=["Reef-safe sunscreen", "Rash guard", "Light rain shell", "Headlamp", "Dry bag",
                    "Sandals + one pair of trainers", "Insect repellent", "USD cash for shuttles"])

BUDGET = dict(lines=[dict(category="Flights", usd=420), dict(category="Hostel beds (14 nights)", usd=316),
                     dict(category="Buses and one shared shuttle", usd=75), dict(category="Food (~$18/day)", usd=252),
                     dict(category="Surf lessons and board rental", usd=200), dict(category="10% buffer", usd=126)],
              total_usd=1389, within_budget=True,
              notes="About $110 under budget, with a buffer for a splurge dinner or an extra lesson.",
              saving_tips=["Cook breakfast and two dinners a week in the hostel kitchen (about -$60)",
                           "Rent a board for the week instead of daily (-$25)",
                           "Take the 6am local bus instead of the shuttle where there is one (-$30)",
                           "Book flights midweek with carry-on only"])

REVIEW_1 = dict(approved=False, issues=[
    "Day 5 is marked 'active' with an early surf lesson, but Nosara → Santa Teresa is ~3 h on unpaved coast road. "
    "Make it a slow travel day and take the shared shuttle."],
    summary="Strong plan; one unrealistic transfer day.")
REVIEW_2 = dict(approved=True, issues=[], summary=(
    "Dates, 14 nights and 3 bases line up; budget lands at $1,389 of $1,500; surf lessons and food are covered; no party hostels. "
    "Day 5 is now a realistic travel day."))

SCRIPTS = {
    "brief": [
        [text("Lots of feeling, few facts: I'll ask the essentials."), tool_use("submit", QUESTIONS)],
        [tool_use("submit", DECISION)],
    ],
    "scout": [
        [text("Thinking Pacific Costa Rica, a Caribbean two-country combo and Siargao. Checking each one."),
         tool_use("geocode", {"place": "Nosara"}), tool_use("geocode", {"place": "Bocas del Toro"}),
         tool_use("geocode", {"place": "Siargao"})],
        [tool_use("climate_for_window", {"lat": 9.9781, "lon": -85.6528, "start_date": "2026-12-01", "end_date": "2026-12-15"}),
         tool_use("climate_for_window", {"lat": 9.3403, "lon": -82.2420, "start_date": "2026-12-01", "end_date": "2026-12-15"}),
         tool_use("climate_for_window", {"lat": 9.8482, "lon": 126.0458, "start_date": "2026-12-01", "end_date": "2026-12-15"})],
        [text("December is dry season in Nicoya but wet on the Caribbean and in Siargao."),
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
    "logistics": [[tool_use("submit", LOG)], [tool_use("submit", LOG)]],
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
        name = next(a.name for a in crew.ALL_AGENTS if a.system + agent_mod.SUBMIT_RULE == system)
        await asyncio.sleep({"brief": 1.2, "judge": 1.5, "itinerary": 2.2, "logistics": 2.6}.get(name, 1.4))
        return resp(*next(self.cursors[name]))


async def fake_run_tool(name, args):
    await asyncio.sleep(0.5)
    key = (name, args.get("place") if name == "geocode" else args["lat"])
    return TOOL_RESULTS[key]


class Recorder:
    """Writes events to a demo recording with `_delay` timing, and keeps them for assertions."""

    def __init__(self, name: str):
        self.path = ROOT / "demo" / name
        self.events: list[dict] = []
        self._last = time.monotonic()

    async def emit(self, ev: dict) -> None:
        now = time.monotonic()
        self.events.append(json.loads(json.dumps({**ev, "_delay": round(now - self._last, 2)}, default=str)))
        self._last = now

    def save(self) -> None:
        self.path.write_text("".join(json.dumps(e) + "\n" for e in self.events))

    def of(self, kind: str) -> list[dict]:
        return [e for e in self.events if e["type"] == kind]


async def main():
    agent_mod._client = NS(messages=FakeMessages())
    agent_mod.run_tool = fake_run_tool

    # Run 1: vague first message -> follow-up questions.
    turns = [ChatTurn(role="user", text=PROMPT)]
    rec_q = Recorder("questions_run.jsonl")
    ev = await pipeline.plan_options(turns, rec_q.emit)
    assert ev["type"] == "questions" and ev["questions"]

    # Run 2: answer -> brief decided -> 3 ranked options.
    turns += [ChatTurn(role="assistant", text="\n".join([ev["reply"], *ev["questions"]])),
              ChatTurn(role="user", text=ANSWER)]
    rec_o = Recorder("options_run.jsonl")
    ev = await pipeline.plan_options(turns, rec_o.emit)
    assert ev["type"] == "options" and len(ev["options"]) == 3
    assert ev["options"][0]["best"] and ev["options"][0]["option"]["name"] == NICOYA
    assert sum(o["best"] for o in ev["options"]) == 1
    for o in ev["options"]:
        rc = o["option"]["rough_cost"]
        assert rc["total_usd"] == rc["flights_usd"] + rc["lodging_usd"] + rc["daily_spend_usd"] + rc["transfers_usd"]
    assert any(len(o["option"]["countries"]) == 2 for o in ev["options"])
    assert all(o["option"]["rough_cost"]["total_usd"] <= ev["brief"]["budget_usd"] for o in ev["options"])
    order = [e["type"] for e in rec_o.events if e["type"] in ("brief", "candidates", "options")]
    assert order == ["brief", "candidates", "options"] and rec_o.of("brief")[0]["followups"]

    # Run 3: plan the best option.
    brief, option = TripBrief.model_validate(ev["brief"]), DestinationOption.model_validate(ev["options"][0]["option"])
    rec_p = Recorder("plan_run.jsonl")
    plan = await pipeline.plan_trip(brief, option, rec_p.emit, notes=[NOTE])
    parts = [e["part"] for e in rec_p.of("plan_part")]
    assert parts == ["itinerary", "logistics", "budget", "itinerary", "logistics", "budget"], parts
    assert plan.review.approved and plan.itinerary.days[4].pace == "slow"
    assert sum(1 for e in rec_p.of("agent_start") if e["agent"] == "logistics") == 2
    assert stay_mismatches(plan.itinerary, plan.logistics, brief.nights) == []
    assert sum(s.nights for s in plan.logistics.stays) == brief.nights
    assert sum(line.usd for line in plan.budget.lines) == plan.budget.total_usd
    assert plan.budget.total_usd <= brief.budget_usd and plan.budget.within_budget and plan.budget.saving_tips
    links = [plan.links.flights, *(s.url for s in plan.links.stays)]
    assert all(u.startswith("https://") for u in links)
    stays = " ".join(s.url for s in plan.links.stays)
    assert all(part in stays for part in ("Nosara", "Santa+Teresa", "S%C3%A1mara"))

    for rec in (rec_q, rec_o, rec_p):
        assert rec.events[0]["type"] == "run_start" and not rec.of("final")
        rec.save()
    print("demo recorded: questions → 3 options →", plan.option_name, len(plan.itinerary.days), "days")


if __name__ == "__main__":
    asyncio.run(main())
