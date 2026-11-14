"""Real-world tools the agents can call. All backed by Open-Meteo (free, no key).

Each tool is a plain async function plus a JSON schema. The same functions are
exposed over MCP in mcp_server.py, so Claude Code (or any MCP client) can use them too.
"""
from __future__ import annotations

from datetime import date, timedelta
from statistics import mean
from typing import Any, Awaitable, Callable

import httpx

_TIMEOUT = httpx.Timeout(15.0)


async def geocode(place: str) -> dict[str, Any]:
    """Resolve a place name to coordinates."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": place, "count": 3, "language": "en"},
        )
        r.raise_for_status()
        results = r.json().get("results") or []
    if not results:
        return {"error": f"No match for '{place}'. Try a more specific or nearby town name."}
    return {
        "matches": [
            {"name": x["name"], "country": x.get("country"), "admin": x.get("admin1"),
             "lat": round(x["latitude"], 4), "lon": round(x["longitude"], 4)}
            for x in results
        ]
    }


async def climate_for_window(lat: float, lon: float, start_date: str, end_date: str) -> dict[str, Any]:
    """Typical weather for a date window, using the same window last year as a proxy."""
    s, e = date.fromisoformat(start_date), date.fromisoformat(end_date)
    last_year = date.today().year - 1
    s_ly, e_ly = s.replace(year=last_year), e.replace(year=last_year if e.year == s.year else last_year + 1)
    if e_ly > date.today() - timedelta(days=5):  # archive lags a few days
        s_ly, e_ly = s_ly.replace(year=s_ly.year - 1), e_ly.replace(year=e_ly.year - 1)
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.get(
            "https://archive-api.open-meteo.com/v1/archive",
            params={
                "latitude": lat, "longitude": lon,
                "start_date": s_ly.isoformat(), "end_date": e_ly.isoformat(),
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,sunshine_duration",
                "timezone": "auto",
            },
        )
        r.raise_for_status()
        d = r.json()["daily"]
    rain = [p or 0 for p in d["precipitation_sum"]]
    sun = [(x or 0) / 3600 for x in d["sunshine_duration"]]
    return {
        "proxy_window": f"{s_ly} to {e_ly}",
        "avg_high_c": round(mean(t for t in d["temperature_2m_max"] if t is not None), 1),
        "avg_low_c": round(mean(t for t in d["temperature_2m_min"] if t is not None), 1),
        "rainy_days": sum(1 for p in rain if p >= 2.0),
        "total_days": len(rain),
        "avg_sunshine_hours": round(mean(sun), 1),
    }


async def ocean_conditions(lat: float, lon: float) -> dict[str, Any]:
    """Current 7-day wave outlook near a coastline (useful for surf/swim vibes)."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.get(
            "https://marine-api.open-meteo.com/v1/marine",
            params={"latitude": lat, "longitude": lon,
                    "daily": "wave_height_max,wave_period_max", "timezone": "auto"},
        )
        if r.status_code != 200:
            return {"error": "No marine data here (likely too far inland). Try coordinates just offshore."}
        d = r.json()["daily"]
    waves = [w for w in d["wave_height_max"] if w is not None]
    periods = [p for p in d["wave_period_max"] if p is not None]
    if not waves:
        return {"error": "No wave data for this point."}
    return {
        "avg_max_wave_m": round(mean(waves), 2),
        "avg_period_s": round(mean(periods), 1) if periods else None,
        "note": "Live 7-day outlook, not a seasonal average.",
    }


ToolFn = Callable[..., Awaitable[dict[str, Any]]]

TOOL_REGISTRY: dict[str, tuple[ToolFn, dict[str, Any]]] = {
    "geocode": (geocode, {
        "name": "geocode",
        "description": "Look up latitude/longitude for a town or region name.",
        "input_schema": {"type": "object", "properties": {"place": {"type": "string"}}, "required": ["place"]},
    }),
    "climate_for_window": (climate_for_window, {
        "name": "climate_for_window",
        "description": "Typical temperature, rain days and sunshine for a location over the travel dates "
                       "(uses last year's same window as a proxy). Call this before claiming anything about weather.",
        "input_schema": {
            "type": "object",
            "properties": {
                "lat": {"type": "number"}, "lon": {"type": "number"},
                "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["lat", "lon", "start_date", "end_date"],
        },
    }),
    "ocean_conditions": (ocean_conditions, {
        "name": "ocean_conditions",
        "description": "Live 7-day wave height/period outlook for a coastal point. Use for surf or swimming plans.",
        "input_schema": {
            "type": "object",
            "properties": {"lat": {"type": "number"}, "lon": {"type": "number"}},
            "required": ["lat", "lon"],
        },
    }),
}


async def run_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Execute a tool; never raise. Errors go back to the model so it can recover."""
    if name not in TOOL_REGISTRY:
        return {"error": f"Unknown tool {name}"}
    fn, _ = TOOL_REGISTRY[name]
    try:
        return await fn(**args)
    except httpx.HTTPError as e:
        return {"error": f"Upstream API failed: {type(e).__name__}. Proceed with best estimate and say so."}
    except Exception as e:  # bad args from the model, etc.
        return {"error": f"{type(e).__name__}: {e}"}
