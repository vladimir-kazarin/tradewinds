"""Tracing must be a no-op without keys and produce a run > agent > generation/tool tree with them."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace as NS

from pydantic import BaseModel

import agent as agent_mod
import tracing
from agent import Agent, Usage


class Recorder:
    def __init__(self):
        self.observations: list[dict] = []
        self._stack: list[str] = []

    @contextmanager
    def start_as_current_observation(self, *, name, as_type="span", **kw):
        obs = {"name": name, "type": as_type, "parent": self._stack[-1] if self._stack else None, **kw}
        self.observations.append(obs)
        self._stack.append(name)
        try:
            yield NS(update=lambda **u: obs.update(u))
        finally:
            self._stack.pop()

    def flush(self):
        pass


class Out(BaseModel):
    answer: str


def _resp(*blocks):
    return NS(content=list(blocks), usage=NS(input_tokens=100, output_tokens=10))


class FakeMessages:
    def __init__(self, responses):
        self.responses = iter(responses)

    async def create(self, **kwargs):
        return next(self.responses)


def test_disabled_tracing_is_a_noop(monkeypatch):
    monkeypatch.setattr(tracing, "_client", lambda: None)
    with tracing.run_trace("plan-options", input={"x": 1}) as t:
        t.update(output="ok")
    with tracing.observation("a", as_type="agent") as o:
        o.update(output="ok")


def test_agent_run_traces_agent_generation_and_tool(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(tracing, "_client", lambda: rec)
    geo = NS(type="tool_use", id="g1", name="geocode", input={"place": "Hanoi"})
    submit = NS(type="tool_use", id="s1", name="submit", input={"answer": "hi"})
    monkeypatch.setattr(agent_mod, "_client", NS(messages=FakeMessages([_resp(geo), _resp(submit)])))

    async def fake_tool(name, args):
        return {"matches": []}

    monkeypatch.setattr(agent_mod, "run_tool", fake_tool)

    async def emit(ev):
        pass

    async def go():
        with tracing.run_trace("plan-options", input={"messages": ["hi"]}, session_id="abc"):
            await Agent(name="scout", role="S", system="s", output=Out, tools=["geocode"]).run("task", emit, Usage())

    asyncio.run(go())
    by_type = {}
    for o in rec.observations:
        by_type.setdefault(o["type"], []).append(o)
    assert by_type["agent"][0]["name"] == "scout" and by_type["agent"][0]["parent"] == "plan-options"
    gens = by_type["generation"]
    assert len(gens) == 2 and all(g["parent"] == "scout" for g in gens)
    assert gens[0]["model"] and gens[0]["usage_details"] == {"input": 100, "output": 10}
    assert gens[0]["cost_details"]["total"] > 0
    assert by_type["tool"][0]["name"] == "geocode" and by_type["tool"][0]["output"] == {"matches": []}
    assert by_type["agent"][0]["output"] == {"answer": "hi"}
