"""Spend and abuse protection for live runs.

A public site with a real API key needs four guards: a per-IP rate limit, a cap
on running + queued runs, a hard cap on what one run may spend, and a daily cap
on total spend. The daily total is persisted to a small JSON file so restarts
don't reset it; the rest is in-memory, which fits a single Fly.io machine.
"""
from __future__ import annotations

import json
import logging
import time
from collections import defaultdict, deque
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

log = logging.getLogger("tradewinds.limits")

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


class DailyBudgetExhausted(RuntimeError):
    """The site's daily spend cap was reached; every live run stops."""


class Reservation:
    """One admitted live run. Release exactly once, however the run ends."""

    def __init__(self, slots: Slots) -> None:
        self._slots = slots
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._slots.in_use -= 1


class Slots:
    """Running + queued live runs. Reserved synchronously at admission, so a burst can't overshoot."""

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self.in_use = 0

    def try_reserve(self) -> Reservation | None:
        if self.in_use >= self.capacity:
            return None
        self.in_use += 1
        return Reservation(self)


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
        cutoff = self._clock() - self.window_s
        for k in [k for k, h in self._hits.items() if h and h[-1] <= cutoff]:
            del self._hits[k]  # forget idle keys so memory stays bounded
        hits = self._hits[key]
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
    """Total estimated spend for the current UTC day, optionally persisted so restarts don't reset it."""

    def __init__(self, daily_limit_usd: float, today: Callable[[], date] = _utc_today,
                 path: Path | None = None) -> None:
        self.daily_limit_usd = daily_limit_usd
        self._today = today
        self._path = path
        self._day = today()
        self._spent = self._load()

    def _load(self) -> float:
        if self._path is None or not self._path.exists():
            return 0.0
        try:
            data = json.loads(self._path.read_text())
            return float(data["spent_usd"]) if data.get("day") == self._day.isoformat() else 0.0
        except (ValueError, KeyError, OSError) as e:
            log.warning("ignoring unreadable spend ledger %s: %s", self._path, e)
            return 0.0

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"day": self._day.isoformat(), "spent_usd": round(self._spent, 6)}))
            tmp.replace(self._path)
        except OSError as e:
            log.warning("could not persist spend ledger: %s", e)

    def _roll(self) -> None:
        if self._today() != self._day:
            self._day, self._spent = self._today(), 0.0

    def add(self, usd: float) -> None:
        self._roll()
        self._spent += usd
        self._save()

    def spent_today(self) -> float:
        self._roll()
        return self._spent

    def exhausted(self) -> bool:
        return self.spent_today() >= self.daily_limit_usd
