"""Runtime contract on models that reject forced tool_choice (Claude Sonnet 5.5 and later)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

from pydantic import BaseModel

import agent as agent_mod
from agent import Agent, Usage


class Out(BaseModel):
    answer: str


def _resp(*blocks):
    return NS(content=list(blocks), usage=NS(input_tokens=1, output_tokens=1))


class FakeMessages:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})  # snapshot: the list keeps growing
        return next(self.responses)


def _run(monkeypatch, responses, max_turns=3):
    fake = FakeMessages(responses)
    monkeypatch.setattr(agent_mod, "_client", NS(messages=fake))
    events: list[dict] = []

    async def emit(ev):
        events.append(ev)

    a = Agent(name="t", role="T", system="Be helpful.", output=Out, max_turns=max_turns)
    out = asyncio.run(a.run("task", emit, Usage()))
    return out, fake.calls


def test_never_forces_tool_choice(monkeypatch):
    submit = NS(type="tool_use", id="s1", name="submit", input={"answer": "hi"})
    out, calls = _run(monkeypatch, [_resp(submit)])
    assert out.answer == "hi"
    assert all(c["tool_choice"] == {"type": "auto"} for c in calls)
    assert "submit" in calls[0]["system"]


def test_text_only_reply_is_nudged_to_submit(monkeypatch):
    chat = NS(type="text", text="Here is my answer: hi")
    submit = NS(type="tool_use", id="s1", name="submit", input={"answer": "hi"})
    out, calls = _run(monkeypatch, [_resp(chat), _resp(submit)])
    assert out.answer == "hi"
    nudge = calls[1]["messages"][-1]
    assert nudge["role"] == "user" and "submit" in str(nudge["content"])


def test_last_turn_is_announced(monkeypatch):
    geo = NS(type="tool_use", id="g1", name="geocode", input={"place": "x"})
    submit = NS(type="tool_use", id="s1", name="submit", input={"answer": "hi"})

    async def fake_tool(name, args):
        return {"ok": True}

    monkeypatch.setattr(agent_mod, "run_tool", fake_tool)
    out, calls = _run(monkeypatch, [_resp(geo), _resp(submit)], max_turns=2)
    last_user = calls[1]["messages"][-1]["content"]
    assert any(b.get("type") == "text" and "final" in b["text"].lower() for b in last_user)
