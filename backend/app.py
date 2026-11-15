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

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator
from sse_starlette.sse import EventSourceResponse

from agent import Emit, Usage
from limits import RateLimiter, RunBudgetExceeded, SpendLedger
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
TOO_BIG_ERROR = "This trip got too big to plan in one go. Try a shorter or simpler trip."
DAILY_LIMIT_MSG = "Tradewinds has reached today's planning limit. Please come back tomorrow."
BUSY_MSG = "Lots of travelers are planning right now. Please try again in a minute."
BUSY_RETRY_S = 30
HOUR_S = 3600
LOG_ONLY = {"usage"}  # recorded for us, never sent to travelers
MAX_TURNS = 8
MAX_PLAN_BODY_CHARS = 30_000  # a real brief + option is ~3k; bigger means abuse
MIN_SPEED = 0.1

RUNS.mkdir(exist_ok=True)
log = logging.getLogger("tradewinds")

# Spend and abuse protection for live runs (demo replays cost nothing and aren't limited).
MAX_CONCURRENT = int(os.getenv("TRADEWINDS_MAX_CONCURRENT", "2"))
MAX_QUEUE = int(os.getenv("TRADEWINDS_MAX_QUEUE", "6"))
RUN_BUDGET_USD = float(os.getenv("TRADEWINDS_RUN_BUDGET_USD", "1.00"))
LEDGER = SpendLedger(float(os.getenv("TRADEWINDS_DAILY_BUDGET_USD", "20")))
OPTIONS_LIMITER = RateLimiter(int(os.getenv("TRADEWINDS_OPTIONS_PER_HOUR", "20")), HOUR_S)
PLAN_LIMITER = RateLimiter(int(os.getenv("TRADEWINDS_PLANS_PER_HOUR", "10")), HOUR_S)
TRUST_PROXY = os.getenv("TRADEWINDS_TRUST_PROXY") == "1"  # set on Fly.io, where Fly-Client-IP is reliable

app = FastAPI(title="Tradewinds")
_live_slots = asyncio.Semaphore(MAX_CONCURRENT)
_inflight = 0  # running + queued live runs


@app.exception_handler(RequestValidationError)
async def _invalid_request(_: Request, exc: RequestValidationError) -> JSONResponse:
    # Never echo the input back: it can be huge or contain values JSON can't encode (NaN).
    errors = [{"loc": list(e.get("loc", [])), "msg": str(e.get("msg", ""))} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})


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

    @model_validator(mode="before")
    @classmethod
    def _bounded_size(cls, data: Any) -> Any:
        if len(json.dumps(data, default=str)) > MAX_PLAN_BODY_CHARS:  # NaN-safe: default json allows nan
            raise ValueError("request body too large")
        return data


def _is_live(demo: bool) -> bool:
    return not demo and bool(os.getenv("ANTHROPIC_API_KEY"))


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "live": bool(os.getenv("ANTHROPIC_API_KEY"))}


def _client_ip(request: Request) -> str:
    if TRUST_PROXY:
        forwarded = request.headers.get("fly-client-ip") or request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _guard(request: Request, limiter: RateLimiter) -> JSONResponse | None:
    """Refuse a live run before it starts: daily cap, overall queue, then per-IP rate."""
    if LEDGER.exhausted():
        return JSONResponse(status_code=503, content={"detail": DAILY_LIMIT_MSG})
    if _inflight >= MAX_CONCURRENT + MAX_QUEUE:
        return JSONResponse(status_code=429, content={"detail": BUSY_MSG},
                            headers={"Retry-After": str(BUSY_RETRY_S)})
    ip = _client_ip(request)
    if not limiter.allow(ip):
        wait = limiter.retry_after(ip)
        minutes = max(1, round(wait / 60))
        msg = f"You've planned a lot in the last hour. Please try again in about {minutes} minutes."
        return JSONResponse(status_code=429, content={"detail": msg}, headers={"Retry-After": str(wait)})
    return None


async def _replay(path: Path, speed: float, prelude: list[dict] | None = None) -> AsyncIterator[dict]:
    for ev in prelude or []:
        yield {"data": json.dumps(ev)}
    for line in path.read_text().splitlines():
        ev = json.loads(line)
        await asyncio.sleep(ev.pop("_delay", 0.4) / speed)
        if ev["type"] not in LOG_ONLY:
            yield {"data": json.dumps(ev)}


Run = Callable[[Emit, Usage], Awaitable[Any]]


async def _live(run: Run) -> AsyncIterator[dict]:
    q: asyncio.Queue = asyncio.Queue()

    async def worker() -> None:
        global _inflight
        _inflight += 1
        try:
            await _run_logged(run, q)
        except Exception:  # anything outside the run itself (log file, disk): still tell the browser
            log.exception("run setup failed")
            await q.put({"type": "error", "message": FRIENDLY_ERROR})
        finally:
            _inflight -= 1
            await q.put(None)

    task = asyncio.create_task(worker())
    try:
        while (ev := await q.get()) is not None:
            yield {"data": json.dumps(ev, default=str)}
    finally:
        task.cancel()  # client disconnected: stop spending tokens


async def _run_logged(run: Run, q: asyncio.Queue) -> None:
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

            usage = Usage(budget_usd=RUN_BUDGET_USD, ledger=LEDGER)
            try:
                await run(emit, usage)
            except RunBudgetExceeded as e:
                f.write(json.dumps({"type": "error", "message": str(e)}) + "\n")
                log.warning("run stopped: %s", e)
                await q.put({"type": "error", "message": TOO_BIG_ERROR})
            except Exception as e:  # surface to UI instead of a dead stream
                f.write(json.dumps({"type": "error", "message": f"{type(e).__name__}: {e}"}) + "\n")
                log.exception("run failed")
                await q.put({"type": "error", "message": FRIENDLY_ERROR})


@app.post("/api/options")
async def options(req: OptionsRequest, request: Request, demo: bool = False, speed: float = 1.0):
    if not _is_live(demo):
        asked = any(t.role == "assistant" for t in req.messages)
        return EventSourceResponse(_replay(DEMO_OPTIONS if asked else DEMO_QUESTIONS, max(speed, MIN_SPEED)))
    refused = _guard(request, OPTIONS_LIMITER)
    if refused is not None:
        return refused
    return EventSourceResponse(_live(lambda emit, usage: plan_options(req.messages, emit, usage)))


def _recorded_option_name() -> str | None:
    for line in DEMO_PLAN.read_text().splitlines():
        ev = json.loads(line)
        if ev["type"] == "plan":
            return ev["option_name"]
    return None


@app.post("/api/plan")
async def plan(req: PlanRequest, request: Request, demo: bool = False, speed: float = 1.0):
    if not _is_live(demo):
        prelude = [] if req.option.name == _recorded_option_name() else [DEMO_NOTE]
        return EventSourceResponse(_replay(DEMO_PLAN, max(speed, MIN_SPEED), prelude))
    refused = _guard(request, PLAN_LIMITER)
    if refused is not None:
        return refused
    return EventSourceResponse(_live(lambda emit, usage: plan_trip(req.brief, req.option, emit, usage)))


@app.get("/")
async def index():
    return FileResponse(FRONTEND / "index.html")


app.mount("/static", StaticFiles(directory=FRONTEND), name="static")
