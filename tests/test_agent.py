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


def test_cancelled_call_is_charged_an_estimate(monkeypatch):
    from limits import SpendLedger

    class Hanging:
        async def create(self, **kwargs):
            raise asyncio.CancelledError()

    monkeypatch.setattr(agent_mod, "_client", NS(messages=Hanging()))
    ledger = SpendLedger(daily_limit_usd=100)

    async def emit(ev):
        pass

    a = Agent(name="t", role="T", system="Be helpful.", output=Out)
    try:
        asyncio.run(a.run("x" * 4000, emit, Usage(ledger=ledger)))
    except asyncio.CancelledError:
        pass
    assert ledger.spent_today() > 0


def test_tool_calls_per_turn_are_capped(monkeypatch):
    calls = [NS(type="tool_use", id=f"g{i}", name="geocode", input={"place": f"p{i}"}) for i in range(20)]
    submit = NS(type="tool_use", id="s1", name="submit", input={"answer": "hi"})
    ran = []

    async def fake_tool(name, args):
        ran.append(args["place"])
        return {"ok": True}

    monkeypatch.setattr(agent_mod, "run_tool", fake_tool)
    out, sent = _run(monkeypatch, [_resp(*calls), _resp(submit)])
    assert len(ran) == agent_mod.MAX_TOOL_CALLS_PER_TURN
    results = sent[1]["messages"][-1]["content"]
    assert len([r for r in results if r.get("type") == "tool_result"]) == 20  # every tool_use still answered


def test_api_errors_reach_ui_without_raw_details(monkeypatch):
    import anthropic, httpx2 as httpx

    class Boom:
        async def create(self, **kwargs):
            raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))

    monkeypatch.setattr(agent_mod, "_client", NS(messages=Boom()))
    events = []

    async def emit(ev):
        events.append(ev)

    try:
        asyncio.run(Agent(name="t", role="T", system="s", output=Out).run("x", emit, Usage()))
    except anthropic.APIConnectionError:
        pass
    err = next(e for e in events if e["type"] == "agent_error")
    assert "api.anthropic.com" not in err["message"]
