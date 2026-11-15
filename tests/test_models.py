"""Model tiers: light work on the cheap model, judgment on the main one."""
from __future__ import annotations

import importlib

import agent
import crew

LIGHT = {"brief", "judge", "budget"}
HEAVY = {"scout", "itinerary", "logistics", "reviewer"}


def test_light_agents_use_the_light_model_and_heavy_agents_the_main_one():
    for a in crew.ALL_AGENTS:
        expected = agent.LIGHT_MODEL if a.name in LIGHT else agent.MODEL
        assert a.model == expected, (a.name, a.model)
    assert {a.name for a in crew.CREW} == LIGHT | HEAVY


def test_tiers_are_configurable(monkeypatch):
    monkeypatch.setenv("TRADEWINDS_MODEL", "claude-opus-5-5")
    monkeypatch.setenv("TRADEWINDS_LIGHT_MODEL", "claude-sonnet-5-5")
    try:
        importlib.reload(agent)
        importlib.reload(crew)
        assert crew.brief_agent.model == "claude-sonnet-5-5" and crew.scout_agent.model == "claude-opus-5-5"
    finally:
        monkeypatch.delenv("TRADEWINDS_MODEL")
        monkeypatch.delenv("TRADEWINDS_LIGHT_MODEL")
        importlib.reload(agent)
        importlib.reload(crew)


def test_light_model_defaults_to_haiku_55():
    assert agent.LIGHT_MODEL == "claude-haiku-5-5"
