"""FastAPI server: streams the crew's work to the browser over Server-Sent Events.

GET /api/plan?prompt=...        live run (needs ANTHROPIC_API_KEY)
GET /api/plan?demo=1            replay a recorded run (no key, no cost)
Every live run is recorded to runs/<timestamp>.jsonl so good runs can become demos.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from crew import plan_trip

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
RUNS = ROOT / "runs"
DEMO = ROOT / "demo" / "sample_run.jsonl"
RUNS.mkdir(exist_ok=True)

app = FastAPI(title="Tradewinds")
_live_slots = asyncio.Semaphore(int(os.getenv("TRADEWINDS_MAX_CONCURRENT", "2")))  # protect the API budget


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "live": bool(os.getenv("ANTHROPIC_API_KEY"))}


async def _replay(speed: float):
    lines = DEMO.read_text().splitlines()
    for line in lines:
        ev = json.loads(line)
        await asyncio.sleep(ev.pop("_delay", 0.4) / speed)
        yield {"data": json.dumps(ev)}


async def _live(prompt: str):
    q: asyncio.Queue = asyncio.Queue()
    log = (RUNS / f"{int(time.time())}.jsonl").open("w")
    last = time.monotonic()

    async def emit(ev: dict) -> None:
        nonlocal last
        now = time.monotonic()
        log.write(json.dumps({**ev, "_delay": round(now - last, 2)}) + "\n")
        log.flush()
        last = now
        await q.put(ev)

    async def worker():
        try:
            async with _live_slots:
                await plan_trip(prompt, emit)
        except Exception as e:  # surface to UI instead of a dead stream
            await emit({"type": "error", "message": f"{type(e).__name__}: {e}"})
        finally:
            await q.put(None)

    task = asyncio.create_task(worker())
    try:
        while (ev := await q.get()) is not None:
            yield {"data": json.dumps(ev, default=str)}
    finally:
        task.cancel()  # client disconnected: stop spending tokens
        log.close()


@app.get("/api/plan")
async def plan(prompt: str = Query("", max_length=1000), demo: bool = False, speed: float = 1.0):
    if demo or not os.getenv("ANTHROPIC_API_KEY"):
        return EventSourceResponse(_replay(max(speed, 0.1)))
    return EventSourceResponse(_live(prompt))


@app.get("/")
async def index():
    return FileResponse(FRONTEND / "index.html")


app.mount("/static", StaticFiles(directory=FRONTEND), name="static")
