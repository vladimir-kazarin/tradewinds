"""Run the eval set against the LIVE pipeline and score it. This spends real API money.

    python evals/run_eval.py --limit 3                 # quick smoke (3 cases, options + plan)
    python evals/run_eval.py --label sonnet55 --judge  # full set, with the Haiku value judge
    python evals/run_eval.py --no-plan                 # step 1 only (much cheaper)
    python evals/run_eval.py --compare A.json B.json   # check-by-check diff of two runs

Each case: send the prompt; if the Brief asks questions, send the scripted answer;
score the 3 options; then plan the best-value option and score the plan. Results
go to evals/results/<timestamp>-<label>.json. With LANGFUSE keys set, every case
is also traced under the session `eval-<label>`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "evals"))

from pydantic import BaseModel, Field  # noqa: E402

import tracing  # noqa: E402
from agent import Agent, Usage  # noqa: E402
from pipeline import plan_options, plan_trip  # noqa: E402
from schemas import ChatTurn, DestinationOption, PlanResult, TripBrief  # noqa: E402
from scoring import score_options, score_plan  # noqa: E402

PROMPTS = ROOT / "evals" / "prompts.jsonl"
RESULTS = ROOT / "evals" / "results"
JUDGE_MODEL = "claude-haiku-5-5"
DEFAULT_CONCURRENCY = 2


class ValueVerdict(BaseModel):
    value: int = Field(ge=1, le=5, description="5 = about as affordable as this kind of trip gets without being grim")
    fit: int = Field(ge=1, le=5, description="5 = clearly what this traveler asked for")
    notes: str = Field(description="One or two sentences: the biggest missed saving or misfit, if any")


judge_agent = Agent(
    name="value_judge", role="Eval judge", output=ValueVerdict, max_turns=3, model=JUDGE_MODEL,
    system=(
        "You grade backpacker trip plans. Given the traveler's brief, the three options offered and the plan for "
        "the best-value option, score value (is this about as cheap as a good version of this trip gets: beds, "
        "transport, gateways, activities) and fit (does it match what they asked for). Be strict and concrete."
    ),
)


def load_cases(limit: int | None, ids: set[str] | None) -> list[dict[str, Any]]:
    cases = [json.loads(line) for line in PROMPTS.read_text().splitlines() if line.strip()]
    if ids:
        cases = [c for c in cases if c["id"] in ids]
    return cases[:limit] if limit else cases


async def _noop_emit(_: dict[str, Any]) -> None:
    return None


MILESTONES = ("brief", "candidates", "options", "plan_part", "plan")


def _clock(t0: float, marks: dict[str, float]):
    """Records when each milestone first arrives: what the traveler actually waits for."""
    async def emit(ev: dict[str, Any]) -> None:
        if ev["type"] in MILESTONES and ev["type"] not in marks:
            marks[ev["type"]] = round(time.monotonic() - t0, 1)
    return emit


def _collector(sink: list[dict[str, Any]], also=None):
    async def emit(ev: dict[str, Any]) -> None:
        if also is not None:
            await also(ev)
        if ev["type"] in ("revision", "agent_done", "agent_error"):
            sink.append({k: v for k, v in ev.items() if k != "output"} | (
                {"output": ev["output"]} if ev.get("agent") in ("reviewer",) else {}))
    return emit


def _judge_input(options_ev: dict[str, Any], plan: PlanResult | None) -> str:
    compact = {
        "brief": options_ev["brief"],
        "options": [{"name": o["option"]["name"], "countries": o["option"]["countries"], "pitch": o["pitch"],
                     "rough_cost": o["option"]["rough_cost"]} for o in options_ev["options"]],
    }
    if plan is not None:
        compact["plan"] = {
            "option": plan.option_name, "headline": plan.itinerary.headline,
            "days": [f"Day {d.day} {d.base}: {d.title}" for d in plan.itinerary.days],
            "stays": [s.model_dump() for s in plan.logistics.stays],
            "flights": plan.logistics.flights.model_dump(), "transfers": [t.model_dump() for t in plan.logistics.transfers],
            "budget": plan.budget.model_dump(),
        }
    return json.dumps(compact, ensure_ascii=False, indent=1)


async def run_case(case: dict[str, Any], *, plan_step: bool, judge: bool, label: str) -> dict[str, Any]:
    usage = Usage()
    t0 = time.monotonic()
    result: dict[str, Any] = {"id": case["id"]}
    try:
        with tracing.run_trace(f"eval:{case['id']}", input={"prompt": case["prompt"]}, session_id=f"eval-{label}"):
            turns = [ChatTurn(role="user", text=case["prompt"])]
            marks: dict[str, float] = {}
            ev = await plan_options(turns, _clock(time.monotonic(), marks), usage)
            result["asked_questions"] = ev["type"] == "questions"
            for _ in range(2):  # the Brief may ask up to two rounds
                if ev["type"] != "questions":
                    break
                turns += [ChatTurn(role="assistant", text="\n".join([ev["reply"], *ev["questions"]])),
                          ChatTurn(role="user", text=case["answer"])]
                t_search = time.monotonic()
                ev = await plan_options(turns, _clock(t_search, marks), usage)
            result["search_waits"] = marks
            result["checks"] = score_options(case["expect"], ev)
            result["options"] = [o["option"]["name"] for o in ev["options"]]
            plan = None
            if plan_step:
                brief = TripBrief.model_validate(ev["brief"])
                best = DestinationOption.model_validate(ev["options"][0]["option"])
                trail: list[dict[str, Any]] = []
                t_plan = time.monotonic()
                plan_marks: dict[str, float] = {}
                plan = await plan_trip(brief, best, _collector(trail, _clock(t_plan, plan_marks)), usage)
                result["plan_waits"] = plan_marks
                result["checks"].update(score_plan(case["expect"], brief, plan))
                result["plan_total_usd"] = plan.budget.total_usd
                result["review"] = plan.review.model_dump()
                result["revisions"] = [e["issues"] for e in trail if e["type"] == "revision"]
                result["agent_seconds"] = [(e["agent"], e["seconds"]) for e in trail if e["type"] == "agent_done"]
            if judge:
                verdict = await judge_agent.run(_judge_input(ev, plan), _noop_emit, usage)
                result["judge"] = verdict.model_dump()
    except Exception as e:  # one broken case must not sink the run; it counts as an error
        result["error"] = f"{type(e).__name__}: {e}"
    result["cost_usd"] = round(usage.cost_usd, 4)
    result["seconds"] = round(time.monotonic() - t0, 1)
    return result


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    names = sorted({k for r in results for k in r.get("checks", {})})
    rates = {}
    for n in names:
        vals = [r["checks"][n] for r in results if n in r.get("checks", {})]
        rates[n] = round(sum(vals) / len(vals), 3) if vals else None
    ok = [r for r in results if "error" not in r]
    judged = [r["judge"] for r in ok if "judge" in r]
    all_pass = [all(r["checks"].values()) for r in ok if r.get("checks")]
    return {
        "cases": len(results), "errors": len(results) - len(ok),
        "all_checks_pass_rate": round(sum(all_pass) / len(results), 3) if results else None,
        "check_pass_rates": rates,
        "judge_value_avg": round(sum(j["value"] for j in judged) / len(judged), 2) if judged else None,
        "judge_fit_avg": round(sum(j["fit"] for j in judged) / len(judged), 2) if judged else None,
        "cost_usd_total": round(sum(r["cost_usd"] for r in results), 3),
        "seconds_avg": round(sum(r["seconds"] for r in results) / len(results), 1) if results else None,
    }


def print_summary(summary: dict[str, Any], results: list[dict[str, Any]]) -> None:
    print(f"\n{summary['cases']} cases, {summary['errors']} errors, "
          f"all-checks pass rate {summary['all_checks_pass_rate']}, cost ${summary['cost_usd_total']}, "
          f"avg {summary['seconds_avg']}s")
    for name, rate in summary["check_pass_rates"].items():
        print(f"  {name:<24} {rate}")
    if summary["judge_value_avg"] is not None:
        print(f"  judge value {summary['judge_value_avg']}/5, fit {summary['judge_fit_avg']}/5")
    for r in results:
        failed = [k for k, v in r.get("checks", {}).items() if not v]
        if "error" in r or failed:
            print(f"  ✗ {r['id']}: {r.get('error') or ', '.join(failed)}")


def compare(a_path: str, b_path: str) -> None:
    a, b = (json.loads(Path(p).read_text()) for p in (a_path, b_path))
    sa, sb = a["summary"], b["summary"]
    print(f"{'metric':<26}{'A':>10}{'B':>10}")
    rows = [("all_checks_pass_rate", sa["all_checks_pass_rate"], sb["all_checks_pass_rate"]),
            ("judge_value_avg", sa["judge_value_avg"], sb["judge_value_avg"]),
            ("judge_fit_avg", sa["judge_fit_avg"], sb["judge_fit_avg"]),
            ("cost_usd_total", sa["cost_usd_total"], sb["cost_usd_total"]),
            ("seconds_avg", sa["seconds_avg"], sb["seconds_avg"])]
    for name in sorted(set(sa["check_pass_rates"]) | set(sb["check_pass_rates"])):
        rows.append((name, sa["check_pass_rates"].get(name), sb["check_pass_rates"].get(name)))
    for name, va, vb in rows:
        print(f"{name:<26}{str(va):>10}{str(vb):>10}")


async def main_async(args: argparse.Namespace) -> None:
    if not os.getenv("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set: the eval runs the live pipeline.")
    cases = load_cases(args.limit, set(args.ids.split(",")) if args.ids else None)
    sem = asyncio.Semaphore(args.concurrency)

    async def one(case: dict[str, Any]) -> dict[str, Any]:
        async with sem:
            r = await run_case(case, plan_step=not args.no_plan, judge=args.judge, label=args.label)
            print(f"  {'✓' if 'error' not in r else '!'} {r['id']} ${r['cost_usd']} {r['seconds']}s", flush=True)
            return r

    print(f"Running {len(cases)} cases (plan step: {not args.no_plan}, judge: {args.judge}) …")
    results = await asyncio.gather(*(one(c) for c in cases))
    summary = summarize(results)
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{time.strftime('%Y%m%d-%H%M%S')}-{args.label}.json"
    out.write_text(json.dumps({"label": args.label, "model": os.getenv("TRADEWINDS_MODEL", "claude-sonnet-5-5"),
                               "summary": summary, "results": results}, indent=1, ensure_ascii=False))
    print_summary(summary, results)
    print(f"\nwrote {out.relative_to(ROOT)}")
    tracing.flush()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--limit", type=int)
    p.add_argument("--ids", help="comma-separated case ids")
    p.add_argument("--no-plan", action="store_true", help="score step 1 only")
    p.add_argument("--judge", action="store_true", help=f"add the {JUDGE_MODEL} value judge")
    p.add_argument("--label", default="run")
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    p.add_argument("--compare", nargs=2, metavar=("A", "B"))
    args = p.parse_args()
    if args.compare:
        compare(*args.compare)
        return
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
