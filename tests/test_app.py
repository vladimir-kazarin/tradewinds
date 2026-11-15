from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
FIRST = {"messages": [{"role": "user", "text": "Somewhere warm"}]}
ANSWERED = {"messages": [{"role": "user", "text": "Somewhere warm"}, {"role": "assistant", "text": "When?"},
                         {"role": "user", "text": "December"}]}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import app
    return TestClient(app.app)


def _events(resp) -> list[dict]:
    assert resp.status_code == 200, resp.text
    return [json.loads(line[5:]) for line in resp.text.splitlines() if line.startswith("data:")]


def _recorded(name: str) -> list[dict]:
    return [json.loads(line) for line in (ROOT / "demo" / name).read_text().splitlines()]


def _plan_body(option_index: int = 0) -> dict:
    opts = next(e for e in _recorded("options_run.jsonl") if e["type"] == "options")
    return {"brief": opts["brief"], "option": opts["options"][option_index]["option"]}


def test_options_demo_first_turn_returns_questions(client):
    evs = _events(client.post("/api/options?speed=100", json=FIRST))
    assert evs[-1]["type"] == "questions"


def test_options_demo_after_answer_returns_options(client):
    evs = _events(client.post("/api/options?speed=100", json=ANSWERED))
    assert evs[-1]["type"] == "options" and len(evs[-1]["options"]) == 3


def test_plan_demo_returns_plan_with_links(client):
    evs = _events(client.post("/api/plan?speed=100", json=_plan_body()))
    assert evs[-1]["type"] == "plan" and evs[-1]["links"]["flights"].startswith("https://")
    assert not any(e.get("text") == "Demo: showing a sample plan" for e in evs)


def test_plan_demo_other_option_shows_demo_note(client):
    evs = _events(client.post("/api/plan?speed=100", json=_plan_body(1)))
    assert evs[0] == {"type": "status", "text": "Demo: showing a sample plan"}


def test_bad_bodies_are_422(client):
    body = _plan_body()
    no_cost = {**body, "option": {k: v for k, v in body["option"].items() if k != "rough_cost"}}
    bad_nights = {**body, "brief": {**body["brief"], "nights": 3}}
    turn = {"role": "user", "text": "hi"}
    cases = [
        ("/api/options", {"messages": []}),
        ("/api/options", {"messages": [turn] * 9}),
        ("/api/options", {"messages": [turn, {"role": "assistant", "text": "q"}]}),
        ("/api/options", {"messages": [{"role": "user", "text": "x" * 1001}]}),
        ("/api/plan", bad_nights),
        ("/api/plan", no_cost),
    ]
    for url, payload in cases:
        assert client.post(url, json=payload).status_code == 422, (url, payload)


def test_usage_events_never_streamed(client):
    for url, body in (("/api/options", ANSWERED), ("/api/plan", _plan_body())):
        evs = _events(client.post(f"{url}?speed=100", json=body))
        assert not any(e["type"] == "usage" for e in evs)
        assert not any("_delay" in e for e in evs)


def test_live_run_hides_usage_and_errors_friendly(monkeypatch, tmp_path):
    import app
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(app, "RUNS", tmp_path)

    async def fake(turns, emit, usage=None):
        await emit({"type": "status", "text": "Reading your trip…"})
        await emit({"type": "usage", "input_tokens": 1, "output_tokens": 1})
        raise RuntimeError("secret internals")

    monkeypatch.setattr(app, "plan_options", fake)
    evs = _events(TestClient(app.app).post("/api/options", json=FIRST))
    assert [e["type"] for e in evs] == ["status", "error"]
    assert "secret" not in evs[-1]["message"]
    logged = (next(tmp_path.glob("*.jsonl"))).read_text()
    assert '"usage"' in logged and "secret internals" in logged


def test_oversized_or_absurd_plan_bodies_are_422(client):
    body = _plan_body()
    cases = [
        {**body, "option": {**body["option"], "why": "x" * 2_000_000}},
        {**body, "brief": {**body["brief"], "end_date": "2036-12-01", "nights": 3653}},
        {**body, "option": {**body["option"], "lat": 123.0}},
    ]
    for payload in cases:
        assert client.post("/api/plan", json=payload).status_code == 422
    raw = json.dumps(body).replace(str(body["option"]["lat"]), "NaN", 1)
    assert client.post("/api/plan", content=raw, headers={"Content-Type": "application/json"}).status_code == 422


def test_live_run_reports_error_when_run_log_cannot_open(monkeypatch, tmp_path):
    import app
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(app, "RUNS", tmp_path / "missing" / "dir")

    async def fake(turns, emit, usage=None):
        await emit({"type": "status", "text": "never"})

    monkeypatch.setattr(app, "plan_options", fake)
    evs = _events(TestClient(app.app).post("/api/options", json=FIRST))
    assert [e["type"] for e in evs] == ["error"]


def _live_app(monkeypatch, tmp_path, **env):
    import app
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(app, "RUNS", tmp_path)
    for k, v in env.items():
        monkeypatch.setattr(app, k, v)

    async def fake(turns, emit, usage=None):
        await emit({"type": "questions", "reply": "hi", "questions": ["when?"]})

    monkeypatch.setattr(app, "plan_options", fake)
    return app


def test_live_requests_are_rate_limited_per_ip(monkeypatch, tmp_path):
    from limits import RateLimiter
    app = _live_app(monkeypatch, tmp_path, OPTIONS_LIMITER=RateLimiter(limit=2, window_s=3600))
    c = TestClient(app.app)
    assert c.post("/api/options", json=FIRST).status_code == 200
    assert c.post("/api/options", json=FIRST).status_code == 200
    r = c.post("/api/options", json=FIRST)
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    assert "detail" in r.json()


def test_forwarded_ip_ignored_unless_proxy_trusted(monkeypatch, tmp_path):
    from limits import RateLimiter
    app = _live_app(monkeypatch, tmp_path, OPTIONS_LIMITER=RateLimiter(limit=1, window_s=3600), TRUST_PROXY=False)
    c = TestClient(app.app)
    assert c.post("/api/options", json=FIRST, headers={"Fly-Client-IP": "1.1.1.1"}).status_code == 200
    assert c.post("/api/options", json=FIRST, headers={"Fly-Client-IP": "2.2.2.2"}).status_code == 429
    monkeypatch.setattr(app, "TRUST_PROXY", True)
    monkeypatch.setattr(app, "OPTIONS_LIMITER", RateLimiter(limit=1, window_s=3600))
    assert c.post("/api/options", json=FIRST, headers={"Fly-Client-IP": "1.1.1.1"}).status_code == 200
    assert c.post("/api/options", json=FIRST, headers={"Fly-Client-IP": "2.2.2.2"}).status_code == 200


def test_daily_budget_exhausted_returns_503(monkeypatch, tmp_path):
    from limits import SpendLedger
    ledger = SpendLedger(daily_limit_usd=1.0)
    ledger.add(1.5)
    app = _live_app(monkeypatch, tmp_path, LEDGER=ledger)
    r = TestClient(app.app).post("/api/options", json=FIRST)
    assert r.status_code == 503 and "detail" in r.json()


def test_demo_mode_is_never_limited(client, monkeypatch):
    import app
    from limits import RateLimiter
    monkeypatch.setattr(app, "OPTIONS_LIMITER", RateLimiter(limit=0, window_s=3600))
    assert client.post("/api/options?speed=100", json=FIRST).status_code == 200


def test_run_over_budget_stops_with_friendly_message(monkeypatch, tmp_path):
    import app
    from types import SimpleNamespace as NS
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(app, "RUNS", tmp_path)
    monkeypatch.setattr(app, "RUN_BUDGET_USD", 0.01)

    async def fake(turns, emit, usage):
        usage.add(NS(input_tokens=100_000, output_tokens=0))  # $0.20 on any priced model
        await emit({"type": "status", "text": "never"})

    monkeypatch.setattr(app, "plan_options", fake)
    evs = _events(TestClient(app.app).post("/api/options", json=FIRST))
    assert evs == [{"type": "error", "message": app.TOO_BIG_ERROR}]


def test_live_run_is_traced_with_session_and_output(monkeypatch, tmp_path):
    import tracing
    from test_tracing import Recorder
    rec = Recorder()
    monkeypatch.setattr(tracing, "_client", lambda: rec)
    app = _live_app(monkeypatch, tmp_path)
    body = {**FIRST, "session_id": "abc-123"}
    _events(TestClient(app.app).post("/api/options", json=body))
    root = rec.observations[0]
    assert root["name"] == "plan-options" and root["parent"] is None
    assert root["input"]["messages"][0]["text"] == "Somewhere warm"
    assert root["output"]["type"] == "questions"


def test_bad_session_id_is_422(client):
    assert client.post("/api/options", json={**FIRST, "session_id": "<script>"}).status_code == 422
