"""Friendly status lines for the UI, derived from agent events.

Travelers see one sentence under the globe, never tool names or raw thoughts.
`with_status` wraps an emit: it forwards every event unchanged and, when an
event means something to a person, follows it with a `status` event. Costs no
tokens: it's a lookup over what the agents are already doing.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from agent import Emit

AGENT_STATUS = {
    "brief": "Reading your trip…",
    "scout": "Looking for places that fit…",
    "judge": "Comparing the options…",
    "itinerary": "Sketching your days…",
    "logistics": "Sorting out flights and stays…",
    "budget": "Adding up the costs…",
    "reviewer": "Double-checking everything…",
}
REVISION_STATUS = "Polishing a few details…"
NEAR_DEGREES = 0.5


def _month(iso: Any) -> str | None:
    try:
        return date.fromisoformat(str(iso)).strftime("%B")
    except ValueError:
        return None


class _Places:
    """Remembers geocoded names so later lat/lon-only calls can be described by place."""

    def __init__(self) -> None:
        self._known: list[tuple[float, float, str]] = []

    def learn(self, result: dict[str, Any]) -> None:
        matches = result.get("matches") or []
        if matches:
            m = matches[0]
            self._known.append((float(m["lat"]), float(m["lon"]), str(m["name"])))

    def near(self, lat: Any, lon: Any) -> str | None:
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            return None
        close = [(abs(klat - lat) + abs(klon - lon), name) for klat, klon, name in self._known
                 if abs(klat - lat) <= NEAR_DEGREES and abs(klon - lon) <= NEAR_DEGREES]
        return min(close)[1] if close else None


def _tool_status(tool: str, args: dict[str, Any], places: _Places) -> str | None:
    if tool == "geocode":
        place = args.get("place")
        return f"Looking at {place}…" if place else "Looking at the map…"
    name = places.near(args.get("lat"), args.get("lon"))
    where = f" near {name}" if name else ""
    if tool == "climate_for_window":
        month = _month(args.get("start_date"))
        return f"Checking {month} weather{where}…" if month else f"Checking the weather{where}…"
    if tool == "ocean_conditions":
        return f"Checking the waves{where}…"
    return None


def with_status(emit: Emit) -> Emit:
    places = _Places()

    def describe(ev: dict[str, Any]) -> str | None:
        kind = ev.get("type")
        if kind == "agent_start":
            return AGENT_STATUS.get(ev.get("agent", ""))
        if kind == "revision":
            return REVISION_STATUS
        if kind == "tool_result" and ev.get("tool") == "geocode" and isinstance(ev.get("result"), dict):
            places.learn(ev["result"])
        if kind == "tool_call":
            return _tool_status(ev.get("tool", ""), ev.get("input") or {}, places)
        return None

    async def wrapped(ev: dict[str, Any]) -> None:
        await emit(ev)
        text = describe(ev)
        if text:
            await emit({"type": "status", "text": text})

    return wrapped
