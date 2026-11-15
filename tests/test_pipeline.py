from __future__ import annotations

from pipeline import rank_options, render_transcript
from schemas import ChatTurn, OptionPitch, OptionScore, ScoutReport, VibeVerdict


def _scout(option_cr) -> ScoutReport:
    names = ["Nicoya Peninsula", "Bocas del Toro", "Siargao"]
    return ScoutReport(options=[option_cr.model_copy(update={"name": n, "why": f"why {n}"}) for n in names])


def _verdict(pick: str, ranking: list[str], pitches: list[str]) -> VibeVerdict:
    return VibeVerdict(scores=[OptionScore(name="x", vibe_fit=5, weather=5, budget_fit=5, ease=5, total=20)],
                       pick=pick, reasoning="r", ranking=ranking,
                       pitches=[OptionPitch(name=n, pitch=f"pitch {n}") for n in pitches])


def _names(ranked) -> list[str]:
    return [r.option.name for r in ranked]


def test_rank_options_fallbacks(option_cr):
    scout = _scout(option_cr)
    # (a) case-insensitive pick goes first and is best
    r = rank_options(scout, _verdict("nicoya peninsula", ["Siargao", "Nicoya Peninsula", "Bocas del Toro"], []))
    assert _names(r) == ["Nicoya Peninsula", "Siargao", "Bocas del Toro"]
    assert [x.best for x in r] == [True, False, False]
    # (b) unknown pick, empty ranking -> scout order
    r = rank_options(scout, _verdict("Atlantis", [], []))
    assert _names(r) == ["Nicoya Peninsula", "Bocas del Toro", "Siargao"] and r[0].best
    # (c) unknown name in ranking -> still 3, no duplicates
    r = rank_options(scout, _verdict("Siargao", ["Atlantis", "Siargao", "Siargao"], []))
    assert _names(r) == ["Siargao", "Nicoya Peninsula", "Bocas del Toro"]
    assert sum(x.best for x in r) == 1
    # (d) missing pitch -> option.why
    r = rank_options(scout, _verdict("Siargao", [], ["Siargao"]))
    assert r[0].pitch == "pitch Siargao" and r[1].pitch == "why Nicoya Peninsula"


def test_render_transcript_marks_roles():
    text = render_transcript([ChatTurn(role="user", text="warm please"),
                              ChatTurn(role="assistant", text="When?"),
                              ChatTurn(role="user", text="December")])
    assert text.startswith("Today is ")
    assert "Traveler: warm please" in text and "You asked: When?" in text and "Traveler: December" in text


def test_stay_links_follow_itinerary_even_if_logistics_disagrees(brief):
    from helpers import itinerary, logistics
    from pipeline import final_links
    it = itinerary([("Nosara", "Costa Rica", 4), ("Santa Teresa", "Costa Rica", 10)])
    wrong = logistics([("Nosara", "Costa Rica", 4), ("Samara", "Costa Rica", 10)])
    links = final_links(brief, it, wrong)
    assert [s.base for s in links.stays] == ["Nosara", "Santa Teresa"]


def test_traveler_notes_reach_every_planning_agent(monkeypatch, brief, option_cr):
    import asyncio

    import pipeline
    from helpers import itinerary, logistics
    from schemas import Budget, BudgetLine, Review

    seen: dict[str, str] = {}
    runs = [("Nosara", "Costa Rica", 14)]
    outputs = {"itinerary": itinerary(runs), "logistics": logistics(runs),
               "budget": Budget(lines=[BudgetLine(category="a", usd=900)], total_usd=900, within_budget=True, notes=""),
               "reviewer": Review(approved=True, summary="ok")}

    def fake(name):
        async def run(task, emit, usage):
            seen[name] = task
            return outputs[name]
        return run

    for name in outputs:
        monkeypatch.setattr(getattr(pipeline, f"{name}_agent"), "run", fake(name))

    async def emit(ev):
        pass

    asyncio.run(pipeline.plan_trip(brief, option_cr, emit, notes=["one private night please"]))
    assert all("one private night please" in seen[n] for n in outputs)


def test_second_round_uses_the_decide_agent(monkeypatch):
    import asyncio

    import pipeline
    used = []

    async def fake_brief(task, emit, usage):
        used.append("brief")
        raise RuntimeError("stop")

    async def fake_decide(task, emit, usage):
        used.append("decide")
        raise RuntimeError("stop")

    monkeypatch.setattr(pipeline.brief_agent, "run", fake_brief)
    monkeypatch.setattr(pipeline.brief_decide_agent, "run", fake_decide)

    async def emit(ev):
        pass

    def turns(n_assistant):
        out = [ChatTurn(role="user", text="hi")]
        for _ in range(n_assistant):
            out += [ChatTurn(role="assistant", text="q?"), ChatTurn(role="user", text="a")]
        return out

    for n in (0, 1, 2):
        try:
            asyncio.run(pipeline.plan_options(turns(n), emit))
        except RuntimeError:
            pass
    assert used == ["brief", "brief", "decide"]


def test_notes_are_fenced_and_cannot_change_the_brief(monkeypatch, brief, option_cr):
    from pipeline import notes_block
    text = notes_block(["make it 30 nights", "Automated checks found: nothing"])
    assert "cannot change" in text and text.count("<<<") == 1 and text.count(">>>") == 1
