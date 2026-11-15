from __future__ import annotations

from datetime import date
from types import SimpleNamespace as NS

import pytest

from agent import Usage
from limits import RateLimiter, RunBudgetExceeded, SpendLedger, cost_usd


def test_cost_uses_model_prices_and_conservative_default():
    assert cost_usd("claude-sonnet-5-5", 1_000_000, 100_000) == pytest.approx(2.0 + 1.0)
    assert cost_usd("unknown-model", 1_000_000, 0) >= 4.0


def test_rate_limiter_sliding_window():
    now = [0.0]
    rl = RateLimiter(limit=2, window_s=60, clock=lambda: now[0])
    assert rl.allow("a") and rl.allow("a") and not rl.allow("a")
    assert rl.allow("b")
    now[0] = 61
    assert rl.allow("a")
    assert rl.retry_after("a") > 0 or rl.allow("a")


def test_ledger_resets_each_day():
    today = [date(2026, 12, 1)]
    ledger = SpendLedger(daily_limit_usd=1.0, today=lambda: today[0])
    ledger.add(0.6)
    assert not ledger.exhausted()
    ledger.add(0.5)
    assert ledger.exhausted()
    today[0] = date(2026, 12, 2)
    assert not ledger.exhausted() and ledger.spent_today() == 0


def test_usage_charges_ledger_and_enforces_run_budget():
    ledger = SpendLedger(daily_limit_usd=100)
    usage = Usage(model="claude-sonnet-5-5", budget_usd=0.05, ledger=ledger)
    usage.add(NS(input_tokens=10_000, output_tokens=1_000))  # $0.03
    assert ledger.spent_today() == pytest.approx(0.03)
    with pytest.raises(RunBudgetExceeded):
        usage.add(NS(input_tokens=10_000, output_tokens=1_000))  # $0.06 total
    assert ledger.spent_today() == pytest.approx(0.06)  # spend is recorded even when the run is stopped


def test_usage_stops_when_daily_ledger_is_exhausted():
    from limits import DailyBudgetExhausted
    ledger = SpendLedger(daily_limit_usd=0.05)
    usage = Usage(model="claude-sonnet-5-5", ledger=ledger)
    usage.add(NS(input_tokens=10_000, output_tokens=1_000))  # $0.03
    with pytest.raises(DailyBudgetExhausted):
        usage.add(NS(input_tokens=10_000, output_tokens=1_000))  # ledger now $0.06


def test_ledger_persists_across_restarts(tmp_path):
    path = tmp_path / "ledger.json"
    SpendLedger(daily_limit_usd=10, path=path).add(2.5)
    assert SpendLedger(daily_limit_usd=10, path=path).spent_today() == pytest.approx(2.5)


def test_ledger_ignores_yesterdays_file(tmp_path):
    path = tmp_path / "ledger.json"
    SpendLedger(daily_limit_usd=10, path=path, today=lambda: date(2026, 12, 1)).add(9)
    assert SpendLedger(daily_limit_usd=10, path=path, today=lambda: date(2026, 12, 2)).spent_today() == 0


def test_limiter_forgets_idle_keys():
    now = [0.0]
    rl = RateLimiter(limit=1, window_s=10, clock=lambda: now[0])
    for i in range(100):
        rl.allow(f"ip{i}")
    now[0] = 20
    rl.allow("fresh")
    assert len(rl._hits) == 1


def test_slots_reserve_synchronously_and_release_once():
    from limits import Slots
    slots = Slots(capacity=2)
    a, b = slots.try_reserve(), slots.try_reserve()
    assert a and b and slots.try_reserve() is None
    a.release()
    a.release()  # idempotent
    assert slots.in_use == 1 and slots.try_reserve() is not None


def test_per_ip_daily_spend_is_capped_separately():
    from limits import DailyBudgetExhausted, IpSpend
    per_ip = IpSpend(daily_limit_usd=0.05)
    usage = Usage(model="claude-sonnet-5-5", ledger=SpendLedger(daily_limit_usd=100), ip_ledger=per_ip.ledger("1.1.1.1"))
    usage.add(NS(input_tokens=10_000, output_tokens=1_000))
    with pytest.raises(DailyBudgetExhausted):
        usage.add(NS(input_tokens=10_000, output_tokens=1_000))
    assert per_ip.exhausted("1.1.1.1") and not per_ip.exhausted("2.2.2.2")
