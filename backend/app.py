"""FastAPI server: streams the crew's work to the browser over Server-Sent Events.

POST /api/options   conversation -> follow-up questions, or 3 ranked options
POST /api/plan      brief + option -> full reviewed plan with booking links
Without ANTHROPIC_API_KEY (or with ?demo=1) both replay recordings from demo/.
Every live run is recorded to runs/<timestamp>.jsonl so good runs can become demos.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import uuid
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from dataclasses import dataclass
from typing import Annotated, Any, AsyncIterator, Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator
from sse_starlette.sse import EventSourceResponse

import tracing
from agent import Emit, Usage
from limits import (DailyBudgetExhausted, IpBudgetExhausted, IpSpend, RateLimiter, Reservation, RunBudgetExceeded,
                    Slots, SpendLedger)
from pipeline import plan_options, plan_trip
from schemas import ChatTurn, DestinationOption, TripBrief

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
RUNS = Path(os.getenv("TRADEWINDS_RUNS_DIR", str(ROOT / "runs")))  # a Fly volume in production
RUNS_KEEP = int(os.getenv("TRADEWINDS_RUNS_KEEP", "500"))
DEMO_DIR = ROOT / "demo"
DEMO_QUESTIONS = DEMO_DIR / "questions_run.jsonl"
DEMO_OPTIONS = DEMO_DIR / "options_run.jsonl"
DEMO_PLAN = DEMO_DIR / "plan_run.jsonl"
DEMO_NOTE = {"type": "status", "text": "Demo: showing a sample plan"}
FRIENDLY_ERROR = "Something went wrong planning this. Please try again."
TOO_BIG_ERROR = "This trip got too big to plan in one go. Try a shorter or simpler trip."
DAILY_LIMIT_MSG = "Tradewinds has reached today's planning limit. Please come back tomorrow."
IP_LIMIT_MSG = "You've used today's planning allowance. Please come back tomorrow."
BUSY_MSG = "Lots of travelers are planning right now. Please try again in a minute."
BUSY_RETRY_S = 30
HOUR_S = 3600
LOG_ONLY = {"usage"}  # recorded for us, never sent to travelers
MAX_TURNS = 8
SESSION_ID = r"^[A-Za-z0-9-]{1,64}$"  # browser-generated id grouping one search's traces
TERMINAL = {"questions", "options", "plan", "error"}
MAX_NOTES = 5
MAX_NOTE_CHARS = 500
MAX_PLAN_BODY_CHARS = 30_000  # a real brief + option is ~3k; bigger means abuse
MIN_SPEED = 0.1

RUNS.mkdir(parents=True, exist_ok=True)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("tradewinds")

CSP = "; ".join([
    "default-src 'self'", "script-src 'self'", "style-src 'self' 'unsafe-inline'",  # Leaflet sets inline styles
    "img-src 'self' data: https://tile.openstreetmap.org https://*.tile.openstreetmap.org",
    "font-src 'self'", "connect-src 'self'", "frame-ancestors 'none'", "base-uri 'self'", "form-action 'self'",
])
SECURITY_HEADERS = {
    "content-security-policy": CSP,
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
    "strict-transport-security": "max-age=31536000; includeSubDomains",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
}
VENDOR_CACHE = "public, max-age=31536000, immutable"


def prune_run_logs(directory: Path, keep: int) -> None:
    """Keep only the newest `keep` run logs so the volume can't fill up."""
    logs = sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in logs[keep:]:
        try:
            old.unlink()
        except OSError as e:
            log.warning("could not delete old run log %s: %s", old, e)

# Spend and abuse protection for live runs (demo replays cost nothing and aren't limited).
MAX_CONCURRENT = int(os.getenv("TRADEWINDS_MAX_CONCURRENT", "2"))
MAX_QUEUE = int(os.getenv("TRADEWINDS_MAX_QUEUE", "6"))
RUN_BUDGET_USD = float(os.getenv("TRADEWINDS_RUN_BUDGET_USD", "1.00"))
LEDGER = SpendLedger(float(os.getenv("TRADEWINDS_DAILY_BUDGET_USD", "20")),
                     path=Path(os.getenv("TRADEWINDS_LEDGER_PATH", str(RUNS / "spend-ledger.json"))))
OPTIONS_LIMITER = RateLimiter(int(os.getenv("TRADEWINDS_OPTIONS_PER_HOUR", "20")), HOUR_S)
PLAN_LIMITER = RateLimiter(int(os.getenv("TRADEWINDS_PLANS_PER_HOUR", "10")), HOUR_S)
TRUST_PROXY = os.getenv("TRADEWINDS_TRUST_PROXY") == "1"  # set on Fly.io, where Fly-Client-IP is reliable
SLOTS = Slots(MAX_CONCURRENT + MAX_QUEUE)
IP_SPEND = IpSpend(float(os.getenv("TRADEWINDS_IP_DAILY_BUDGET_USD", "3")))
HIDDEN_EVENTS = {"thought", "tool_call", "tool_result"}  # recorded for us, not for travelers
IPV6_PREFIX = 64  # one person usually controls a whole /64

@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    prune_run_logs(RUNS, RUNS_KEEP)
    yield
    tracing.flush()  # send buffered traces before the process exits


app = FastAPI(title="Tradewinds", lifespan=_lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def _headers(request: Request, call_next: Callable[[Request], Awaitable[Any]]) -> Any:
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    path = request.url.path
    if path.startswith("/static/vendor/"):
        response.headers["cache-control"] = VENDOR_CACHE
    elif path == "/" or path.startswith("/static/"):
        response.headers["cache-control"] = "no-cache"  # revalidate app code on every deploy
    return response
_live_slots = asyncio.Semaphore(MAX_CONCURRENT)


@app.exception_handler(RequestValidationError)
async def _invalid_request(_: Request, exc: RequestValidationError) -> JSONResponse:
    # Never echo the input back: it can be huge or contain values JSON can't encode (NaN).
    errors = [{"loc": list(e.get("loc", [])), "msg": str(e.get("msg", ""))} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})


class OptionsRequest(BaseModel):
    messages: list[ChatTurn] = Field(min_length=1, max_length=MAX_TURNS)
    session_id: str | None = Field(None, pattern=SESSION_ID)

    @model_validator(mode="after")
    def _last_from_user(self) -> OptionsRequest:
        if self.messages[-1].role != "user":
            raise ValueError("the last message must be from the user")
        return self


class PlanRequest(BaseModel):
    brief: TripBrief
    option: DestinationOption
    session_id: str | None = Field(None, pattern=SESSION_ID)
    notes: list[Annotated[str, Field(min_length=1, max_length=MAX_NOTE_CHARS)]] = Field(
        default_factory=list, max_length=MAX_NOTES)  # what the traveler added while the search ran

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
    """Rate-limit key. Only Fly's own header is trusted (X-Forwarded-For is client-controlled);
    IPv6 addresses are grouped by /64 so one host can't rotate through addresses."""
    raw = request.headers.get("fly-client-ip", "") if TRUST_PROXY else ""
    raw = raw.strip() or (request.client.host if request.client else "unknown")
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return raw
    if ip.version == 6:
        return str(ipaddress.ip_network(f"{ip}/{IPV6_PREFIX}", strict=False))
    return str(ip)


def _admit(request: Request, limiter: RateLimiter) -> Reservation | JSONResponse:
    """Admit a live run or refuse it: daily cap, then running+queued slots, then per-IP rate.
    The slot is reserved here, synchronously, so a burst of requests can't overshoot the cap."""
    if LEDGER.exhausted():
        return JSONResponse(status_code=503, content={"detail": DAILY_LIMIT_MSG})
    ip = _client_ip(request)
    if IP_SPEND.exhausted(ip):
        return JSONResponse(status_code=429, content={"detail": IP_LIMIT_MSG})
    if not limiter.allow(ip):
        wait = limiter.retry_after(ip)
        minutes = max(1, round(wait / 60))
        msg = f"You've planned a lot in the last hour. Please try again in about {minutes} minutes."
        return JSONResponse(status_code=429, content={"detail": msg}, headers={"Retry-After": str(wait)})
    reservation = SLOTS.try_reserve()
    if reservation is None:
        return JSONResponse(status_code=429, content={"detail": BUSY_MSG},
                            headers={"Retry-After": str(BUSY_RETRY_S)})
    reservation.ip = ip
    return reservation


def _for_browser(ev: dict) -> dict | None:
    """Strip what travelers never see: tool traffic, thoughts, full agent outputs, raw API errors."""
    kind = ev.get("type")
    if kind in LOG_ONLY or kind in HIDDEN_EVENTS:
        return None
    if kind == "agent_done":
        return {k: v for k, v in ev.items() if k != "output"}
    if kind == "agent_error":
        return {"type": "agent_error", "agent": ev.get("agent")}
    return ev


async def _replay(path: Path, speed: float, prelude: list[dict] | None = None) -> AsyncIterator[dict]:
    for ev in prelude or []:
        yield {"data": json.dumps(ev)}
    for line in path.read_text().splitlines():
        ev = json.loads(line)
        await asyncio.sleep(ev.pop("_delay", 0.4) / speed)
        shown = _for_browser(ev)
        if shown is not None:
            yield {"data": json.dumps(shown)}


Run = Callable[[Emit, Usage], Awaitable[Any]]


@dataclass(frozen=True)
class TraceInfo:
    name: str
    input: Any
    session_id: str | None


async def _live(run: Run, trace: TraceInfo, reservation: Reservation) -> AsyncIterator[dict]:
    q: asyncio.Queue = asyncio.Queue()

    async def worker() -> None:
        try:
            await _run_logged(run, q, trace, IP_SPEND.ledger(getattr(reservation, "ip", "unknown")))
        except Exception:  # anything outside the run itself (log file, disk): still tell the browser
            log.exception("run setup failed")
            await q.put({"type": "error", "message": FRIENDLY_ERROR})
        finally:
            reservation.release()
            await q.put(None)

    task = asyncio.create_task(worker())
    try:
        while (ev := await q.get()) is not None:
            yield {"data": json.dumps(ev, default=str)}
    finally:
        task.cancel()  # client disconnected: stop spending tokens
        reservation.release()


async def _run_logged(run: Run, q: asyncio.Queue, trace: TraceInfo, ip_ledger: SpendLedger | None = None) -> None:
    async with _live_slots:
        if LEDGER.exhausted():  # the cap may have been reached while this run was queued
            await q.put({"type": "error", "message": DAILY_LIMIT_MSG})
            return
        # Opened only once a slot is free, so queued runs leave no empty files.
        with (RUNS / f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}.jsonl").open("w") as f:
            last = time.monotonic()

            async def emit(ev: dict) -> None:
                nonlocal last
                now = time.monotonic()
                f.write(json.dumps({**ev, "_delay": round(now - last, 2)}, default=str) + "\n")
                f.flush()
                last = now
                if ev["type"] in TERMINAL:
                    root.update(output=ev)
                shown = _for_browser(ev)
                if shown is not None:
                    await q.put(shown)

            usage = Usage(budget_usd=RUN_BUDGET_USD, ledger=LEDGER, ip_ledger=ip_ledger)
            try:
                with tracing.run_trace(trace.name, input=trace.input, session_id=trace.session_id,
                                       metadata={"run_log": f.name.rsplit("/", 1)[-1]}) as root:
                    await run(emit, usage)
            except IpBudgetExhausted as e:
                f.write(json.dumps({"type": "error", "message": str(e)}) + "\n")
                log.warning("run stopped: %s", e)
                await q.put({"type": "error", "message": IP_LIMIT_MSG})
            except DailyBudgetExhausted as e:
                f.write(json.dumps({"type": "error", "message": str(e)}) + "\n")
                log.warning("run stopped: %s", e)
                await q.put({"type": "error", "message": DAILY_LIMIT_MSG})
            except RunBudgetExceeded as e:
                f.write(json.dumps({"type": "error", "message": str(e)}) + "\n")
                log.warning("run stopped: %s", e)
                await q.put({"type": "error", "message": TOO_BIG_ERROR})
            except Exception as e:  # surface to UI instead of a dead stream
                f.write(json.dumps({"type": "error", "message": f"{type(e).__name__}: {e}"}) + "\n")
                log.exception("run failed")
                await q.put({"type": "error", "message": FRIENDLY_ERROR})
    prune_run_logs(RUNS, RUNS_KEEP)


@app.post("/api/options")
async def options(req: OptionsRequest, request: Request, demo: bool = False, speed: float = 1.0):
    if not _is_live(demo):
        asked = any(t.role == "assistant" for t in req.messages)
        return EventSourceResponse(_replay(DEMO_OPTIONS if asked else DEMO_QUESTIONS, max(speed, MIN_SPEED)))
    admitted = _admit(request, OPTIONS_LIMITER)
    if isinstance(admitted, JSONResponse):
        return admitted
    trace = TraceInfo("plan-options", {"messages": [t.model_dump() for t in req.messages]}, req.session_id)
    return EventSourceResponse(_live(lambda emit, usage: plan_options(req.messages, emit, usage), trace, admitted))


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
    admitted = _admit(request, PLAN_LIMITER)
    if isinstance(admitted, JSONResponse):
        return admitted
    trace = TraceInfo("plan-trip", {"brief": req.brief.model_dump(mode="json"), "option": req.option.name},
                      req.session_id)
    return EventSourceResponse(_live(lambda emit, usage: plan_trip(req.brief, req.option, emit, usage, req.notes),
                                     trace, admitted))


@app.get("/")
async def index():
    return FileResponse(FRONTEND / "index.html")


app.mount("/static", StaticFiles(directory=FRONTEND), name="static")
