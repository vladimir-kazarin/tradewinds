"""FastAPI server: streams the crew's work to the browser over Server-Sent Events.

POST /api/options   conversation -> follow-up questions, or 3 ranked options
POST /api/plan      brief + option -> full reviewed plan with booking links
Without ANTHROPIC_API_KEY (or with ?demo=1) both replay recordings from demo/.
Every live run is recorded to runs/<timestamp>.jsonl so good runs can become demos.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator
from sse_starlette.sse import EventSourceResponse

from agent import Emit
from pipeline import plan_options, plan_trip
from schemas import ChatTurn, DestinationOption, TripBrief

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
RUNS = ROOT / "runs"
DEMO_DIR = ROOT / "demo"
DEMO_QUESTIONS = DEMO_DIR / "questions_run.jsonl"
DEMO_OPTIONS = DEMO_DIR / "options_run.jsonl"
DEMO_PLAN = DEMO_DIR / "plan_run.jsonl"
DEMO_NOTE = {"type": "status", "text": "Demo: showing a sample plan"}
FRIENDLY_ERROR = "Something went wrong planning this. Please try again."
LOG_ONLY = {"usage"}  # recorded for us, never sent to travelers
MAX_TURNS = 8
MIN_SPEED = 0.1

RUNS.mkdir(exist_ok=True)
log = logging.getLogger("tradewinds")

app = FastAPI(title="Tradewinds")
_live_slots = asyncio.Semaphore(int(os.getenv("TRADEWINDS_MAX_CONCURRENT", "2")))  # protect the API budget


class OptionsRequest(BaseModel):
    messages: list[ChatTurn] = Field(min_length=1, max_length=MAX_TURNS)

    @model_validator(mode="after")
    def _last_from_user(self) -> OptionsRequest:
        if self.messages[-1].role != "user":
            raise ValueError("the last message must be from the user")
        return self


class PlanRequest(BaseModel):
    brief: TripBrief
    option: DestinationOption


def _is_live(demo: bool) -> bool:
    return not demo and bool(os.getenv("ANTHROPIC_API_KEY"))


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "live": bool(os.getenv("ANTHROPIC_API_KEY"))}


async def _replay(path: Path, speed: float, prelude: list[dict] | None = None) -> AsyncIterator[dict]:
    for ev in prelude or []:
        yield {"data": json.dumps(ev)}
    for line in path.read_text().splitlines():
        ev = json.loads(line)
        await asyncio.sleep(ev.pop("_delay", 0.4) / speed)
        if ev["type"] not in LOG_ONLY:
            yield {"data": json.dumps(ev)}


async def _live(run: Callable[[Emit], Awaitable[Any]]) -> AsyncIterator[dict]:
    q: asyncio.Queue = asyncio.Queue()

    async def worker() -> None:
        try:
            async with _live_slots:
                # Opened only once a slot is free, so queued runs leave no empty files.
                with (RUNS / f"{int(time.time() * 1000)}.jsonl").open("w") as f:
                    last = time.monotonic()

                    async def emit(ev: dict) -> None:
                        nonlocal last
                        now = time.monotonic()
                        f.write(json.dumps({**ev, "_delay": round(now - last, 2)}, default=str) + "\n")
                        f.flush()
                        last = now
                        if ev["type"] not in LOG_ONLY:
                            await q.put(ev)

                    try:
                        await run(emit)
                    except Exception as e:  # surface to UI instead of a dead stream
                        f.write(json.dumps({"type": "error", "message": f"{type(e).__name__}: {e}"}) + "\n")
                        log.exception("run failed")
                        await q.put({"type": "error", "message": FRIENDLY_ERROR})
        finally:
            await q.put(None)

    task = asyncio.create_task(worker())
    try:
        while (ev := await q.get()) is not None:
            yield {"data": json.dumps(ev, default=str)}
    finally:
        task.cancel()  # client disconnected: stop spending tokens


@app.post("/api/options")
async def options(req: OptionsRequest, demo: bool = False, speed: float = 1.0):
    if not _is_live(demo):
        asked = any(t.role == "assistant" for t in req.messages)
        return EventSourceResponse(_replay(DEMO_OPTIONS if asked else DEMO_QUESTIONS, max(speed, MIN_SPEED)))
    return EventSourceResponse(_live(lambda emit: plan_options(req.messages, emit)))


def _recorded_option_name() -> str | None:
    for line in DEMO_PLAN.read_text().splitlines():
        ev = json.loads(line)
        if ev["type"] == "plan":
            return ev["option_name"]
    return None


@app.post("/api/plan")
async def plan(req: PlanRequest, demo: bool = False, speed: float = 1.0):
    if not _is_live(demo):
        prelude = [] if req.option.name == _recorded_option_name() else [DEMO_NOTE]
        return EventSourceResponse(_replay(DEMO_PLAN, max(speed, MIN_SPEED), prelude))
    return EventSourceResponse(_live(lambda emit: plan_trip(req.brief, req.option, emit)))


@app.get("/")
async def index():
    return FileResponse(FRONTEND / "index.html")


app.mount("/static", StaticFiles(directory=FRONTEND), name="static")
