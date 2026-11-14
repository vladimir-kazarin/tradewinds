"""A minimal agent runtime on the raw Anthropic SDK, no framework.

Loop: call the model -> run any tool_use blocks (in parallel) -> feed tool_result
back -> repeat until the model calls its `submit` tool. The submit tool's
input_schema is a Pydantic model, so the final answer is validated structured
data. If validation fails, the error goes back to the model to fix.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Generic, TypeVar

from anthropic import AsyncAnthropic, APIStatusError, APIConnectionError
from pydantic import BaseModel, ValidationError

from tools import TOOL_REGISTRY, run_tool

T = TypeVar("T", bound=BaseModel)
Emit = Callable[[dict[str, Any]], Awaitable[None]]

MODEL = os.getenv("TRADEWINDS_MODEL", "claude-sonnet-5-5")
_client: AsyncAnthropic | None = None


def client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(max_retries=3)  # SDK handles 429/5xx backoff
    return _client


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, u: Any) -> None:
        self.input_tokens += u.input_tokens
        self.output_tokens += u.output_tokens


@dataclass
class Agent(Generic[T]):
    name: str
    role: str                      # short label for the UI
    system: str
    output: type[T]
    tools: list[str] = field(default_factory=list)
    max_turns: int = 8
    max_tokens: int = 4096

    def _submit_tool(self) -> dict[str, Any]:
        return {
            "name": "submit",
            "description": f"Submit your final {self.output.__name__}. Call exactly once, when done.",
            "input_schema": self.output.model_json_schema(),
        }

    async def run(self, task: str, emit: Emit, usage: Usage) -> T:
        tools = [TOOL_REGISTRY[t][1] for t in self.tools] + [self._submit_tool()]
        messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
        await emit({"type": "agent_start", "agent": self.name, "role": self.role})
        t0 = time.monotonic()

        for turn in range(self.max_turns):
            # On the last turn, force the submit tool so we always terminate.
            tool_choice = {"type": "tool", "name": "submit"} if turn == self.max_turns - 1 else {"type": "any"}
            try:
                resp = await client().messages.create(
                    model=MODEL, max_tokens=self.max_tokens, system=self.system,
                    tools=tools, tool_choice=tool_choice, messages=messages,
                )
            except (APIStatusError, APIConnectionError) as e:
                await emit({"type": "agent_error", "agent": self.name, "message": str(e)})
                raise
            usage.add(resp.usage)
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
            outs = await asyncio.gather(*(run_tool(c.name, c.input) for c in real))
            for c, o in zip(real, outs):
                await emit({"type": "tool_result", "agent": self.name, "tool": c.name, "result": o})
                results.append({"type": "tool_result", "tool_use_id": c.id, "content": json.dumps(o),
                                "is_error": "error" in o})
            # Every tool_use needs a result, even ones we skipped.
            answered = {r["tool_use_id"] for r in results}
            for c in calls:
                if c.id not in answered:
                    results.append({"type": "tool_result", "tool_use_id": c.id, "content": "skipped"})
            messages.append({"role": "user", "content": results})

        raise RuntimeError(f"{self.name} did not submit within {self.max_turns} turns")
