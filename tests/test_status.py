from __future__ import annotations

import asyncio

from status import with_status

NOSARA_GEO = {"type": "tool_result", "agent": "scout", "tool": "geocode",
              "result": {"matches": [{"name": "Nosara", "country": "Costa Rica", "lat": 9.9781, "lon": -85.6528}]}}


def _run(events: list[dict]) -> list[dict]:
    out: list[dict] = []

    async def sink(ev: dict) -> None:
        out.append(ev)

    async def go() -> None:
        emit = with_status(sink)
        for ev in events:
            await emit(ev)

    asyncio.run(go())
    return out


def _statuses(out: list[dict]) -> list[str]:
    return [e["text"] for e in out if e["type"] == "status"]


def test_forwards_every_event_first():
    ev = {"type": "agent_start", "agent": "scout", "role": "Destination Scout"}
    out = _run([ev])
    assert out[0] == ev and out[1]["type"] == "status"
    assert _run([{"type": "thought", "agent": "scout", "text": "x"}]) == [{"type": "thought", "agent": "scout", "text": "x"}]


def test_agent_start_maps_to_sentence():
    assert _statuses(_run([{"type": "agent_start", "agent": "judge", "role": "Vibe Judge"}])) == ["Comparing the options…"]


def test_climate_uses_geocoded_name_and_month():
    call = {"type": "tool_call", "agent": "scout", "tool": "climate_for_window",
            "input": {"lat": 9.95, "lon": -85.68, "start_date": "2026-12-01", "end_date": "2026-12-15"}}
    assert _statuses(_run([NOSARA_GEO, call])) == ["Checking December weather near Nosara…"]


def test_unknown_location_falls_back():
    call = {"type": "tool_call", "agent": "itinerary", "tool": "ocean_conditions", "input": {"lat": 40.0, "lon": 10.0}}
    texts = _statuses(_run([NOSARA_GEO, call]))
    assert texts and "Nosara" not in texts[0]


def test_status_text_never_contains_tool_names():
    events = [{"type": "agent_start", "agent": a, "role": a} for a in
              ("brief", "scout", "judge", "itinerary", "logistics", "budget", "reviewer")]
    events += [NOSARA_GEO,
               {"type": "tool_call", "agent": "scout", "tool": "geocode", "input": {"place": "Nosara"}},
               {"type": "tool_call", "agent": "scout", "tool": "climate_for_window",
                "input": {"lat": 1, "lon": 1, "start_date": "2026-03-01", "end_date": "2026-03-05"}},
               {"type": "tool_call", "agent": "itinerary", "tool": "ocean_conditions", "input": {"lat": 9.97, "lon": -85.6}},
               {"type": "revision", "round": 1, "issues": ["x"]}]
    texts = _statuses(_run(events))
    assert len(texts) == 11
    for t in texts:
        for banned in ("geocode", "climate", "ocean_conditions", "submit"):
            assert banned not in t


def test_nearest_known_place_wins():
    st = {"type": "tool_result", "agent": "itinerary", "tool": "geocode",
          "result": {"matches": [{"name": "Santa Teresa", "country": "Costa Rica", "lat": 9.6464, "lon": -85.1686}]}}
    call = {"type": "tool_call", "agent": "itinerary", "tool": "ocean_conditions", "input": {"lat": 9.64, "lon": -85.19}}
    assert _statuses(_run([NOSARA_GEO, st, call])) == ["Checking the waves near Santa Teresa…"]
