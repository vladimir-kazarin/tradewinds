"""A minimal agent runtime on the raw Anthropic SDK, no framework.

Loop: call the model -> run any tool_use blocks (in parallel) -> feed tool_result
back -> repeat until the model calls its `submit` tool. Current models (e.g.
Claude Sonnet 5.5) reject forced tool_choice, so tool_choice stays `auto`: the
system prompt requires `submit`, a text-only reply gets a nudge, and the last
turn is announced. The submit tool's
input_schema is a Pydantic model, so the final answer is validated structured
data. If validation fails, the error goes back to the model to fix.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Generic, TypeVar

from anthropic import AsyncAnthropic, APIStatusError, APIConnectionError
from pydantic import BaseModel, ValidationError

import tracing
from limits import DailyBudgetExhausted, RunBudgetExceeded, SpendLedger, cost_usd
from tools import TOOL_REGISTRY, run_tool

T = TypeVar("T", bound=BaseModel)
Emit = Callable[[dict[str, Any]], Awaitable[None]]

MODEL = os.getenv("TRADEWINDS_MODEL", "claude-sonnet-5-5")  # judgment-heavy agents
LIGHT_MODEL = os.getenv("TRADEWINDS_LIGHT_MODEL", "claude-haiku-5-5")  # extraction, scoring, arithmetic
SUBMIT_RULE = ("\n\nWhen you are done, call the `submit` tool exactly once with your final answer. "
               "Never finish with a text-only reply: your work only counts when it arrives through `submit`.")
CHARS_PER_TOKEN = 4  # rough estimate, only used to charge a cancelled call
NUDGE = "You replied without calling a tool. Call the `submit` tool now with your final answer."
FINAL_TURN = "This is your final turn: call the `submit` tool now with your best final answer."
_client: AsyncAnthropic | None = None


def client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(max_retries=3)  # SDK handles 429/5xx backoff
    return _client


@dataclass
class Usage:
    """Tokens and estimated cost for one run, charged to the daily ledger as they happen."""
    model: str = MODEL
    budget_usd: float | None = None
    ledger: SpendLedger | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, u: Any, model: str | None = None) -> None:
        self.input_tokens += u.input_tokens
        self.output_tokens += u.output_tokens
        delta = cost_usd(model or self.model, u.input_tokens, u.output_tokens)
        self.cost_usd += delta
        if self.ledger is not None:
            self.ledger.add(delta)
            if self.ledger.exhausted():
                raise DailyBudgetExhausted(f"daily spend ${self.ledger.spent_today():.2f} reached its cap")
        if self.budget_usd is not None and self.cost_usd > self.budget_usd:
            raise RunBudgetExceeded(f"run spent ${self.cost_usd:.3f}, cap is ${self.budget_usd:.2f}")


@dataclass
class Agent(Generic[T]):
    name: str
    role: str                      # short label for the UI
    system: str
    output: type[T]
    tools: list[str] = field(default_factory=list)
    max_turns: int = 8
    max_tokens: int = 16_000  # adaptive thinking counts against this, so leave room
    model: str = MODEL

    def _submit_tool(self) -> dict[str, Any]:
        return {
            "name": "submit",
            "description": f"Submit your final {self.output.__name__}. Call exactly once, when done.",
            "input_schema": self.output.model_json_schema(),
        }

    async def run(self, task: str, emit: Emit, usage: Usage) -> T:
        with tracing.observation(self.name, as_type="agent", input=task,
                                 metadata={"role": self.role, "tools": self.tools}) as span:
            out = await self._loop(task, emit, usage)
            span.update(output=json.loads(out.model_dump_json()))
            return out

    async def _call_model(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], turn: int,
                          emit: Emit, usage: Usage) -> Any:
        system = self.system + SUBMIT_RULE
        trace_input = {"system": system, "messages": tracing.jsonable(messages)} if tracing.enabled() else None
        with tracing.observation(f"{self.name} · turn {turn + 1}", as_type="generation", model=self.model,
                                 input=trace_input, model_parameters={"max_tokens": self.max_tokens}) as gen:
            try:
                resp = await client().messages.create(
                    model=self.model, max_tokens=self.max_tokens, system=system,
                    tools=tools, tool_choice={"type": "auto"}, messages=messages,
                )
            except (APIStatusError, APIConnectionError) as e:
                await emit({"type": "agent_error", "agent": self.name, "message": str(e)})
                raise
            except asyncio.CancelledError:
                # The request may still be billed; charge a conservative estimate so the daily cap sees it.
                est_in = len(system + json.dumps(tracing.jsonable(messages), default=str)) // CHARS_PER_TOKEN
                usage.add(SimpleNamespace(input_tokens=est_in, output_tokens=self.max_tokens), self.model)
                raise
            u = resp.usage
            charge_error: Exception | None = None
            try:
                usage.add(u, self.model)  # charge first: tracing must never skip the ledger
            except (RunBudgetExceeded, DailyBudgetExhausted) as e:
                charge_error = e
            gen.update(output=tracing.jsonable(resp.content) if tracing.enabled() else None,
                       usage_details={"input": u.input_tokens, "output": u.output_tokens},
                       cost_details={"total": cost_usd(self.model, u.input_tokens, u.output_tokens)})
            if charge_error is not None:
                raise charge_error
            return resp

    async def _run_tool(self, call: Any) -> dict[str, Any]:
        with tracing.observation(call.name, as_type="tool", input=call.input) as span:
            out = await run_tool(call.name, call.input)
            span.update(output=out, **({"level": "WARNING"} if "error" in out else {}))
            return out

    async def _loop(self, task: str, emit: Emit, usage: Usage) -> T:
        tools = [TOOL_REGISTRY[t][1] for t in self.tools] + [self._submit_tool()]
        messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
        await emit({"type": "agent_start", "agent": self.name, "role": self.role})
        t0 = time.monotonic()

        for turn in range(self.max_turns):
            resp = await self._call_model(messages, tools, turn, emit, usage)
            messages.append({"role": "assistant", "content": resp.content})

            for block in resp.content:
                if block.type == "text" and block.text.strip():
                    await emit({"type": "thought", "agent": self.name, "text": block.text.strip()[:400]})

            calls = [b for b in resp.content if b.type == "tool_use"]
            results: list[dict[str, Any]] = []

            # Submit first: validate, return or bounce the error back to the model.
            submit = next((c for c in calls if c.name == "submit"), None)
            if submit is not None:
                try:
                    out = self.output.model_validate(submit.input)
                    await emit({"type": "agent_done", "agent": self.name,
                                "seconds": round(time.monotonic() - t0, 1),
                                "output": json.loads(out.model_dump_json())})
                    return out
                except ValidationError as e:
                    await emit({"type": "thought", "agent": self.name, "text": "Output failed validation, retrying."})
                    results.append({"type": "tool_result", "tool_use_id": submit.id, "is_error": True,
                                    "content": f"Validation failed, fix and resubmit:\n{e}"})

            # Run real tool calls concurrently.
            real = [c for c in calls if c.name != "submit"]
            for c in real:
                await emit({"type": "tool_call", "agent": self.name, "tool": c.name, "input": c.input})
            outs = await asyncio.gather(*(self._run_tool(c) for c in real))
            for c, o in zip(real, outs):
                await emit({"type": "tool_result", "agent": self.name, "tool": c.name, "result": o})
                results.append({"type": "tool_result", "tool_use_id": c.id, "content": json.dumps(o),
                                "is_error": "error" in o})
            # Every tool_use needs a result, even ones we skipped.
            answered = {r["tool_use_id"] for r in results}
            for c in calls:
                if c.id not in answered:
                    results.append({"type": "tool_result", "tool_use_id": c.id, "content": "skipped"})
            next_is_last = turn + 1 == self.max_turns - 1
            if not calls:
                messages.append({"role": "user", "content": FINAL_TURN if next_is_last else NUDGE})
                continue
            if next_is_last:
                results.append({"type": "text", "text": FINAL_TURN})
            messages.append({"role": "user", "content": results})

        raise RuntimeError(f"{self.name} did not submit within {self.max_turns} turns")
