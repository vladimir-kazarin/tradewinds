"""Langfuse tracing for live runs, written against the raw SDK loop (no framework).

Tree per run:  run (plan-options | plan-trip)
                 └ agent (scout, judge, …)
                     ├ generation (one per model call: model, messages, usage, cost)
                     └ tool (geocode, climate_for_window, …)

Everything is a no-op unless LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are
set (LANGFUSE_BASE_URL picks the region), so tests, demo mode and local runs
without an account behave exactly as before.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from functools import lru_cache
from types import SimpleNamespace
from typing import Any, Iterator

_NOOP = SimpleNamespace(update=lambda **_: None)


@lru_cache(maxsize=1)
def _client() -> Any | None:
    if not (os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")):
        return None
    from langfuse import get_client  # imported lazily: only needed when tracing is on

    return get_client()


def enabled() -> bool:
    return _client() is not None


def jsonable(obj: Any) -> Any:
    """SDK content blocks, dicts and lists -> plain JSON-ish data for the trace UI."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json", exclude_none=True)
    if isinstance(obj, SimpleNamespace):
        return {k: jsonable(v) for k, v in vars(obj).items()}
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    return obj


@contextmanager
def observation(name: str, *, as_type: str = "span", **fields: Any) -> Iterator[Any]:
    """A child observation of whatever is current. Errors are marked on it and re-raised."""
    client = _client()
    if client is None:
        yield _NOOP
        return
    with client.start_as_current_observation(name=name, as_type=as_type, **fields) as obs:
        try:
            yield obs
        except BaseException as e:
            obs.update(level="ERROR", status_message=f"{type(e).__name__}: {e}")
            raise


@contextmanager
def run_trace(name: str, *, input: Any, session_id: str | None = None,
              metadata: dict[str, Any] | None = None) -> Iterator[Any]:
    """Root observation for one live run; session_id groups a traveler's options + plans."""
    client = _client()
    if client is None:
        yield _NOOP
        return
    from langfuse import propagate_attributes

    with observation(name, as_type="span", input=input) as root:
        with propagate_attributes(session_id=session_id, trace_name=name, metadata=metadata):
            yield root


def flush() -> None:
    client = _client()
    if client is not None:
        client.flush()
