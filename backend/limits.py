"""Spend and abuse protection for live runs.

A public site with a real API key needs three guards: a per-IP rate limit, a
hard cap on what one run may spend, and a daily cap on total spend. All three
are in-memory, which fits a single Fly.io machine; they reset on restart.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from datetime import date, datetime, timezone
from typing import Callable

# USD per million tokens (input, output). Unknown models use the conservative default.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
DEFAULT_PRICE_PER_MTOK = (10.00, 50.00)
MTOK = 1_000_000


class RunBudgetExceeded(RuntimeError):
    """A single run spent more than its cap; the pipeline stops."""


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    price_in, price_out = PRICES_PER_MTOK.get(model, DEFAULT_PRICE_PER_MTOK)
    return (input_tokens * price_in + output_tokens * price_out) / MTOK


class RateLimiter:
    """Sliding-window limit of `limit` events per `window_s` seconds, per key."""

    def __init__(self, limit: int, window_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str) -> deque[float]:
        hits = self._hits[key]
        cutoff = self._clock() - self.window_s
        while hits and hits[0] <= cutoff:
            hits.popleft()
        return hits

    def allow(self, key: str) -> bool:
        hits = self._prune(key)
        if len(hits) >= self.limit:
            return False
        hits.append(self._clock())
        return True

    def retry_after(self, key: str) -> int:
        hits = self._prune(key)
        if not hits:
            return 1
        return max(1, int(hits[0] + self.window_s - self._clock()) + 1)


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


class SpendLedger:
    """Total estimated spend for the current UTC day."""

    def __init__(self, daily_limit_usd: float, today: Callable[[], date] = _utc_today) -> None:
        self.daily_limit_usd = daily_limit_usd
        self._today = today
        self._day = today()
        self._spent = 0.0

    def _roll(self) -> None:
        if self._today() != self._day:
            self._day, self._spent = self._today(), 0.0

    def add(self, usd: float) -> None:
        self._roll()
        self._spent += usd

    def spent_today(self) -> float:
        self._roll()
        return self._spent

    def exhausted(self) -> bool:
        return self.spent_today() >= self.daily_limit_usd
