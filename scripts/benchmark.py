#!/usr/bin/env python3
"""Headless core performance benchmark and regression comparator for git-ontology-agent.

Measures four core dimensions on a fixed task suite (`benchmarks/tasks.json`) with no
network access and no UI/terminal focus:
  1. Relation classification accuracy (k1..k5, multi-open target selection, clarify)
  2. Dependency invalidation accuracy (exact match, precision/recall/F1, preserved claims)
  3. 3-way claim merge accuracy (type rules, conflict resolution, git merge invariants)
  4. End-to-end scenario pass rate & request processing latency (submit ack, tick,
     full scenario, and headless `goa serve` JSON-lines round-trip)

Usage:
  python3 scripts/benchmark.py                                      # run and print summary
  python3 scripts/benchmark.py --json --output result.json          # write machine-readable JSON
  python3 scripts/benchmark.py --write-baseline                     # record benchmarks/baseline.json
  python3 scripts/benchmark.py --compare benchmarks/baseline.json   # run and compare (exit 1 on regression)
  python3 scripts/benchmark.py --compare-files base.json curr.json  # compare two JSON files
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import io
import json
import math
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from goa import Agent, mock  # noqa: E402
from goa.ontology import INVALID, SUPERSEDED, Claim, State, merge3  # noqa: E402
from goa.serve import Server  # noqa: E402

SCHEMA_VERSION = 1
DEFAULT_TASKS_PATH = ROOT / "benchmarks" / "tasks.json"
DEFAULT_BASELINE_PATH = ROOT / "benchmarks" / "baseline.json"
ALL_SUITES = ("relation", "invalidation", "merge", "scenarios")

SUSPECT_MODULES = {
    "relation_accuracy": ["goa/decision.py", "goa/agent.py", "goa/skills.py"],
    "invalidation_exact_match": ["goa/ontology.py", "goa/agent.py", "goa/skills.py"],
    "invalidation_f1": ["goa/ontology.py", "goa/agent.py"],
    "merge_accuracy": ["goa/ontology.py", "goa/agent.py", "goa/gitstore.py"],
    "scenario_pass_rate": ["goa/agent.py", "goa/skills.py", "goa/respond.py", "goa/mock.py", "goa/serve.py"],
    "overall_accuracy": ["goa/agent.py", "goa/ontology.py", "goa/decision.py"],
    "submit_ack_p50_ms": ["goa/agent.py", "goa/decision.py", "goa/gitstore.py", "goa/respond.py"],
    "tick_p50_ms": ["goa/agent.py", "goa/gitstore.py", "goa/skills.py"],
    "scenario_total_p50_ms": ["goa/agent.py", "goa/gitstore.py", "goa/ontology.py", "goa/mock.py"],
    "serve_roundtrip_p50_ms": ["goa/serve.py", "goa/agent.py", "goa/gitstore.py"],
}


def _block_network(*_args: Any, **_kwargs: Any) -> None:
    raise RuntimeError("network access is blocked during headless benchmark execution")


@contextlib.contextmanager
def _isolated_env() -> Any:
    """Block network calls and strip GOA_/provider env vars so local .env never affects runs."""
    orig_urlopen = urllib.request.urlopen
    orig_env = dict(os.environ)
    clean = {
        k: v for k, v in orig_env.items()
        if k not in mock.FAKE_KEYS and not k.startswith("GOA_")
    }
    clean["GOA_ENV_FILE"] = os.devnull
    clean["NO_COLOR"] = "1"
    os.environ.clear()
    os.environ.update(clean)
    urllib.request.urlopen = _block_network  # type: ignore[assignment]
    try:
        yield
    finally:
        urllib.request.urlopen = orig_urlopen  # type: ignore[assignment]
        os.environ.clear()
        os.environ.update(orig_env)


def _git_output(*args: str) -> str:
    try:
        res = subprocess.run(
            ["git", "-C", str(ROOT), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        return res.stdout.strip() if res.returncode == 0 else ""
    except OSError:
        return ""


def _git_metadata(base_branch: str = "preconquer-gogoymh") -> Dict[str, Any]:
    commit = _git_output("rev-parse", "HEAD")
    short_commit = _git_output("rev-parse", "--short", "HEAD")
    branch = _git_output("rev-parse", "--abbrev-ref", "HEAD")
    base_commit = _git_output("rev-parse", "--verify", f"{base_branch}^{{commit}}")
    dirty = bool(_git_output("status", "--porcelain"))
    return {
        "commit": commit,
        "short_commit": short_commit,
        "branch": branch,
        "base_branch": base_branch,
        "base_commit": base_commit,
        "dirty": dirty,
    }


def _percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    idx = (len(ordered) - 1) * (pct / 100.0)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return round(ordered[lo], 3)
    frac = idx - lo
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * frac, 3)


def _latency_stats(samples: Sequence[float]) -> Dict[str, Any]:
    if not samples:
        return {"count": 0, "min": 0.0, "p50": 0.0, "p95": 0.0, "mean": 0.0, "max": 0.0}
    return {
        "count": len(samples),
        "min": round(min(samples), 3),
        "p50": _percentile(samples, 50.0),
        "p95": _percentile(samples, 95.0),
        "mean": round(statistics.mean(samples), 3),
        "max": round(max(samples), 3),
    }


def _build_state(raw_claims: Sequence[Dict[str, Any]]) -> State:
    return State(Claim.from_dict(c) for c in raw_claims)


# --- Suite 1: Relation Classification ----------------------------------------

def run_relation_suite(cases: Sequence[Dict[str, Any]], case_filter: Optional[str] = None) -> Dict[str, Any]:
    selected = [c for c in cases if not case_filter or c["id"] == case_filter]
    results: List[Dict[str, Any]] = []
    by_rel: Dict[str, Dict[str, int]] = {}

    for case in selected:
        tmp = tempfile.mkdtemp(prefix="goa-bench-rel-")
        try:
            agent = Agent(tmp, llm="offline", decision="local", lang=case["lang"])
            for turn in case["setup"]:
                agent.submit(turn["message"])
                for _ in range(turn.get("ticks", 0)):
                    agent.tick()

            t0 = time.perf_counter()
            answers = agent.submit(case["followup"])
            elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 3)

            ans = answers[0] if answers else None
            actual_rel = "clarify" if agent.pending is not None else (ans.relation if ans else "")
            actual_target = ""
            if ans and ans.source:
                actual_target = ans.source.split("@", 1)[0]
                if actual_target.startswith("cancelled/"):
                    actual_target = actual_target[len("cancelled/"):]

            passed = actual_rel == case["expected_relation"]
            details: Dict[str, Any] = {
                "expected_relation": case["expected_relation"],
                "actual_relation": actual_rel,
            }

            if "expected_target_branch" in case:
                details["expected_target_branch"] = case["expected_target_branch"]
                details["actual_target_branch"] = actual_target
                passed = passed and (actual_target == case["expected_target_branch"])

            if case["expected_relation"] == "clarify" and "clarify_reply" in case:
                post_answers = agent.submit(case["clarify_reply"])
                post_rel = post_answers[0].relation if post_answers else ""
                details["expected_post_clarify_relation"] = case["expected_post_clarify_relation"]
                details["actual_post_clarify_relation"] = post_rel
                passed = passed and (post_rel == case["expected_post_clarify_relation"])

            exp = case["expected_relation"]
            bucket = by_rel.setdefault(exp, {"total": 0, "passed": 0})
            bucket["total"] += 1
            if passed:
                bucket["passed"] += 1

            results.append({
                "id": case["id"],
                "passed": passed,
                "elapsed_ms": elapsed_ms,
                **details,
            })
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    passed_n = sum(1 for r in results if r["passed"])
    total = len(results)
    by_rel_summary = {
        k: {
            "total": v["total"],
            "passed": v["passed"],
            "accuracy": round(v["passed"] / v["total"], 4) if v["total"] else 0.0,
        }
        for k, v in sorted(by_rel.items())
    }
    return {
        "total": total,
        "passed": passed_n,
        "accuracy": round(passed_n / total, 4) if total else 0.0,
        "by_relation": by_rel_summary,
        "cases": results,
    }


# --- Suite 2: Dependency Invalidation ----------------------------------------

def run_invalidation_suite(cases: Sequence[Dict[str, Any]], case_filter: Optional[str] = None) -> Dict[str, Any]:
    selected = [c for c in cases if not case_filter or c["id"] == case_filter]
    results: List[Dict[str, Any]] = []
    tp_total = fp_total = fn_total = 0

    for case in selected:
        mode = case["mode"]
        t0 = time.perf_counter()
        extra_ok = True
        extra_errors: List[str] = []

        if mode == "state_invalidate":
            state = _build_state(case["initial_claims"])
            for raw in case.get("add_claims", []):
                state.put(Claim.from_dict(raw))
            actual_invalid = state.invalidate(case["changed_ids"])
            actual_confirmed = [c.id for c in state if c.status == "confirmed"]
        elif mode == "state_correct":
            state = _build_state(case["initial_claims"])
            old, actual_invalid = state.correct(Claim.from_dict(case["correction"]))
            actual_confirmed = [c.id for c in state if c.status == "confirmed"]
            sup_id = case.get("expected_superseded_id")
            if sup_id:
                sup = state.get(sup_id)
                if sup is None or sup.status != SUPERSEDED:
                    extra_ok = False
                    extra_errors.append(f"expected {sup_id} to be superseded")
            updated = state.get(case["correction"]["id"])
            if "expected_prev" in case and (updated is None or updated.prev != case["expected_prev"]):
                extra_ok = False
                extra_errors.append(f"expected prev={case['expected_prev']!r}, got {updated.prev if updated else None!r}")
            if "expected_corrects" in case and (updated is None or updated.corrects != case["expected_corrects"]):
                extra_ok = False
                extra_errors.append(
                    f"expected corrects={case['expected_corrects']!r}, got {updated.corrects if updated else None!r}"
                )
            if old is None and "expected_prev" in case:
                extra_ok = False
                extra_errors.append("expected old claim to be returned by correct()")
        elif mode == "agent_flow":
            tmp = tempfile.mkdtemp(prefix="goa-bench-inv-")
            try:
                agent = Agent(tmp, llm="offline", decision="local", lang=case["lang"])
                for turn in case["setup"]:
                    agent.submit(turn["message"])
                    for _ in range(turn.get("ticks", 0)):
                        agent.tick()
                agent.submit(case["followup"])
                st = agent.repo.read_state(case["branch"])
                actual_invalid = [c.id for c in st if c.status == INVALID]
                actual_confirmed = [c.id for c in st if c.status == "confirmed"]

                sup_id = case.get("expected_superseded_id")
                if sup_id:
                    sup = st.get(sup_id)
                    if sup is None or sup.status != SUPERSEDED:
                        extra_ok = False
                        extra_errors.append(f"expected {sup_id} to be superseded on {case['branch']}")

                agent.run()
                main = agent.repo.read_state("main")
                still_invalid = [c.id for c in main if c.status == INVALID]
                if still_invalid:
                    extra_ok = False
                    extra_errors.append(f"invalid claims remained on main after run: {still_invalid}")
                for cid in case.get("expected_after_run_confirmed", []):
                    cl = main.get(cid)
                    if cl is None or cl.status != "confirmed":
                        extra_ok = False
                        extra_errors.append(f"expected {cid} confirmed on main after run")
                for cid, exp_from in (case.get("expected_preserved_from") or {}).items():
                    cl = main.get(cid)
                    if cl is None or cl.from_ != exp_from:
                        extra_ok = False
                        extra_errors.append(
                            f"expected {cid}.from == {exp_from!r}, got {cl.from_ if cl else None!r}"
                        )
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        else:
            raise ValueError(f"unknown invalidation mode: {mode!r}")

        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 3)

        exp_inv = set(case["expected_invalid"])
        act_inv = set(actual_invalid)
        tp = len(exp_inv & act_inv)
        fp = len(act_inv - exp_inv)
        fn = len(exp_inv - act_inv)
        tp_total += tp
        fp_total += fp
        fn_total += fn

        exp_conf = set(case.get("expected_confirmed", []))
        act_conf = set(actual_confirmed)
        missing_confirmed = sorted(exp_conf - act_conf)
        if missing_confirmed:
            extra_ok = False
            extra_errors.append(f"expected confirmed missing: {missing_confirmed}")

        exact = (act_inv == exp_inv) and extra_ok
        results.append({
            "id": case["id"],
            "mode": mode,
            "passed": exact,
            "elapsed_ms": elapsed_ms,
            "expected_invalid": sorted(exp_inv),
            "actual_invalid": sorted(act_inv),
            "errors": extra_errors,
        })

    passed_n = sum(1 for r in results if r["passed"])
    total = len(results)
    precision = round(tp_total / (tp_total + fp_total), 4) if (tp_total + fp_total) else 1.0
    recall = round(tp_total / (tp_total + fn_total), 4) if (tp_total + fn_total) else 1.0
    f1 = round((2 * precision * recall) / (precision + recall), 4) if (precision + recall) else 0.0
    return {
        "total": total,
        "passed": passed_n,
        "exact_match_rate": round(passed_n / total, 4) if total else 0.0,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "cases": results,
    }


# --- Suite 3: 3-Way Claim Merge ----------------------------------------------

def run_merge_suite(cases: Sequence[Dict[str, Any]], case_filter: Optional[str] = None) -> Dict[str, Any]:
    selected = [c for c in cases if not case_filter or c["id"] == case_filter]
    results: List[Dict[str, Any]] = []

    for case in selected:
        base = _build_state(case["base"])
        ours = _build_state(case["ours"])
        theirs = _build_state(case["theirs"])
        judge = None
        if "judge_score" in case:
            score = float(case["judge_score"])
            judge = lambda _e, _l, s=score: s  # noqa: E731
        threshold = float(case.get("threshold", 0.7))

        t0 = time.perf_counter()
        merged, conflicts = merge3(base, ours, theirs, judge=judge, threshold=threshold)
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 3)

        errors: List[str] = []
        for cid, expect in case["expected_claims"].items():
            actual = merged.get(cid)
            if actual is None:
                errors.append(f"missing expected claim {cid}")
                continue
            for field, exp_val in expect.items():
                act_val = actual.from_ if field == "from" else getattr(actual, field)
                if act_val != exp_val:
                    errors.append(f"{cid}.{field}: expected {exp_val!r}, got {act_val!r}")

        for cid in case.get("expected_absent", []):
            if merged.get(cid) is not None:
                errors.append(f"claim {cid} should be absent after merge")

        if len(conflicts) != case["expected_conflict_count"]:
            errors.append(f"conflicts: expected {case['expected_conflict_count']}, got {len(conflicts)}")

        unresolved = sum(1 for c in conflicts if not c.resolved)
        if unresolved != case["expected_unresolved_count"]:
            errors.append(f"unresolved conflicts: expected {case['expected_unresolved_count']}, got {unresolved}")

        results.append({
            "id": case["id"],
            "passed": not errors,
            "elapsed_ms": elapsed_ms,
            "conflicts": len(conflicts),
            "unresolved": unresolved,
            "errors": errors,
        })

    passed_n = sum(1 for r in results if r["passed"])
    total = len(results)
    return {
        "total": total,
        "passed": passed_n,
        "accuracy": round(passed_n / total, 4) if total else 0.0,
        "cases": results,
    }


# --- Suite 4: End-to-End Scenarios & Request Processing Latency --------------

def _run_agent_scenario_once(case: Dict[str, Any]) -> Tuple[bool, List[str], Dict[str, Any]]:
    tmp = tempfile.mkdtemp(prefix="goa-bench-e2e-")
    submit_samples: List[float] = []
    tick_samples: List[float] = []
    errors: List[str] = []
    try:
        ctx = mock.session() if case.get("mock") else contextlib.nullcontext(None)
        t_total_start = time.perf_counter()
        with ctx:
            agent = Agent(tmp, llm=case["llm"], decision=case["decision"], lang=case["lang"])
            answers = []
            for turn in case["turns"]:
                t_sub = time.perf_counter()
                answers += agent.submit(turn["message"])
                submit_samples.append((time.perf_counter() - t_sub) * 1000.0)

                ticks = turn.get("ticks")
                if ticks is None:
                    while agent.open_tasks():
                        t_tk = time.perf_counter()
                        answers += agent.tick()
                        tick_samples.append((time.perf_counter() - t_tk) * 1000.0)
                else:
                    for _ in range(ticks):
                        t_tk = time.perf_counter()
                        answers += agent.tick()
                        tick_samples.append((time.perf_counter() - t_tk) * 1000.0)
        total_ms = (time.perf_counter() - t_total_start) * 1000.0

        actual_rels = [a.relation for a in answers]
        if actual_rels != case["expected_relations"]:
            errors.append(f"relations: expected {case['expected_relations']}, got {actual_rels}")

        last_text = answers[-1].text if answers else ""
        for sub in case["expected_final_substrings"]:
            if sub not in last_text:
                errors.append(f"missing substring in final answer: {sub!r}")

        merges_raw = agent.repo.git("log", "--merges", "--format=%s", "main").splitlines()
        merges = [m for m in merges_raw if m.strip()]
        if len(merges) != case["expected_merges"]:
            errors.append(f"merge commits: expected {case['expected_merges']}, got {len(merges)}")

        cancelled = sorted(agent.repo.branches("cancelled/"))
        if cancelled != sorted(case.get("expected_cancelled_branches", [])):
            errors.append(
                f"cancelled branches: expected {case.get('expected_cancelled_branches', [])}, got {cancelled}"
            )

        main_state = agent.repo.read_state("main")
        inv_on_main = [c.id for c in main_state if c.status == INVALID]
        if inv_on_main:
            errors.append(f"invalid claims on main: {inv_on_main}")

        return not errors, errors, {
            "submit_ms": submit_samples,
            "tick_ms": tick_samples,
            "total_ms": total_ms,
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run_serve_scenario_once(case: Dict[str, Any]) -> Tuple[bool, List[str], Dict[str, Any]]:
    tmp = tempfile.mkdtemp(prefix="goa-bench-serve-")
    errors: List[str] = []
    try:
        lines = "\n".join(json.dumps({"type": "submit", "text": m}, ensure_ascii=False) for m in case["messages"]) + "\n"
        inp = io.StringIO(lines)
        out = io.StringIO()
        ctx = mock.session() if case.get("mock") else contextlib.nullcontext(None)

        t_start = time.perf_counter()
        with ctx:
            server = Server(
                lambda on_answer, on_step: Agent(
                    tmp,
                    llm=case["llm"],
                    decision=case["decision"],
                    lang=case["lang"],
                    on_answer=on_answer,
                    on_step=on_step,
                ),
                inp,
                out,
            )
            rc = server.run()
        total_ms = (time.perf_counter() - t_start) * 1000.0

        if rc != 0:
            errors.append(f"serve exit code {rc}")
        events = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
        types = [e.get("type") for e in events]
        if not types or types[0] != "ready":
            errors.append("missing initial ready event")
        if "error" in types:
            errors.append(f"serve emitted error event: {[e for e in events if e.get('type') == 'error']}")
        answers = [e for e in events if e.get("type") == "answer"]
        final_answers = [e for e in answers if e.get("kind") == "final"]
        if not final_answers:
            errors.append("missing final answer event")
        else:
            last_text = final_answers[-1].get("text", "")
            for sub in case["expected_final_substrings"]:
                if sub not in last_text:
                    errors.append(f"missing substring in serve final answer: {sub!r}")
        task_events = [e for e in events if e.get("type") == "tasks"]
        if not task_events or task_events[-1].get("open") != []:
            errors.append("serve did not reach idle tasks state")

        merges_raw = server.agent.repo.git("log", "--merges", "--format=%s", "main").splitlines()
        merges = [m for m in merges_raw if m.strip()]
        if len(merges) != case["expected_merges"]:
            errors.append(f"serve merge commits: expected {case['expected_merges']}, got {len(merges)}")

        return not errors, errors, {"serve_ms": total_ms}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_scenarios_and_latency(
    cases: Sequence[Dict[str, Any]],
    iterations: int = 3,
    case_filter: Optional[str] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    selected = [c for c in cases if not case_filter or c["id"] == case_filter]
    submit_all: List[float] = []
    tick_all: List[float] = []
    scenario_all: List[float] = []
    serve_all: List[float] = []
    by_scenario: Dict[str, Dict[str, Any]] = {}
    case_results: List[Dict[str, Any]] = []

    iters = max(1, iterations)
    for case in selected:
        runner = _run_serve_scenario_once if case["mode"] == "serve" else _run_agent_scenario_once
        # Single warmup run when taking multiple measured samples
        all_ok = True
        first_errors: List[str] = []
        if iters >= 2:
            ok_warm, err_warm, _ = runner(case)
            all_ok = ok_warm
            first_errors = list(err_warm)

        per_case_totals: List[float] = []
        for _ in range(iters):
            ok, errs, timing = runner(case)
            if not ok:
                all_ok = False
                if not first_errors:
                    first_errors = errs
            if case["mode"] == "serve":
                serve_all.append(timing["serve_ms"])
                per_case_totals.append(timing["serve_ms"])
            else:
                submit_all.extend(timing["submit_ms"])
                tick_all.extend(timing["tick_ms"])
                scenario_all.append(timing["total_ms"])
                per_case_totals.append(timing["total_ms"])

        stats = _latency_stats(per_case_totals)
        by_scenario[case["id"]] = stats
        case_results.append({
            "id": case["id"],
            "mode": case["mode"],
            "passed": all_ok,
            "p50_ms": stats["p50"],
            "p95_ms": stats["p95"],
            "mean_ms": stats["mean"],
            "errors": first_errors,
        })

    passed_n = sum(1 for r in case_results if r["passed"])
    total = len(case_results)
    suite_report = {
        "total": total,
        "passed": passed_n,
        "pass_rate": round(passed_n / total, 4) if total else 0.0,
        "cases": case_results,
    }
    latency_report = {
        "iterations": iters,
        "submit_ack_ms": _latency_stats(submit_all),
        "tick_ms": _latency_stats(tick_all),
        "scenario_total_ms": _latency_stats(scenario_all),
        "serve_roundtrip_ms": _latency_stats(serve_all),
        "by_scenario_ms": by_scenario,
    }
    return suite_report, latency_report


# --- Runner & Comparator -----------------------------------------------------

def run_benchmark(
    tasks_path: Path = DEFAULT_TASKS_PATH,
    suites: Sequence[str] = ALL_SUITES,
    iterations: int = 3,
    case_filter: Optional[str] = None,
    base_branch: str = "preconquer-gogoymh",
) -> Dict[str, Any]:
    with open(tasks_path, "r", encoding="utf-8") as f:
        tasks = json.load(f)

    suite_set = set(suites)
    suite_reports: Dict[str, Any] = {}
    latency_report: Dict[str, Any] = {
        "iterations": iterations,
        "submit_ack_ms": _latency_stats([]),
        "tick_ms": _latency_stats([]),
        "scenario_total_ms": _latency_stats([]),
        "serve_roundtrip_ms": _latency_stats([]),
        "by_scenario_ms": {},
    }

    with _isolated_env():
        if "relation" in suite_set:
            suite_reports["relation"] = run_relation_suite(tasks["relation_cases"], case_filter)
        if "invalidation" in suite_set:
            suite_reports["invalidation"] = run_invalidation_suite(tasks["invalidation_cases"], case_filter)
        if "merge" in suite_set:
            suite_reports["merge"] = run_merge_suite(tasks["merge_cases"], case_filter)
        if "scenarios" in suite_set:
            sc_rep, latency_report = run_scenarios_and_latency(
                tasks["scenario_cases"], iterations=iterations, case_filter=case_filter
            )
            suite_reports["scenarios"] = sc_rep

    total_cases = sum(s["total"] for s in suite_reports.values())
    passed_cases = sum(s["passed"] for s in suite_reports.values())
    overall_acc = round(passed_cases / total_cases, 4) if total_cases else 0.0

    summary: Dict[str, Any] = {
        "overall_accuracy": overall_acc,
        "total_cases": total_cases,
        "passed_cases": passed_cases,
    }
    if "relation" in suite_reports:
        summary["relation_accuracy"] = suite_reports["relation"]["accuracy"]
    if "invalidation" in suite_reports:
        summary["invalidation_exact_match"] = suite_reports["invalidation"]["exact_match_rate"]
        summary["invalidation_f1"] = suite_reports["invalidation"]["f1"]
    if "merge" in suite_reports:
        summary["merge_accuracy"] = suite_reports["merge"]["accuracy"]
    if "scenarios" in suite_reports:
        summary["scenario_pass_rate"] = suite_reports["scenarios"]["pass_rate"]
        summary["submit_ack_p50_ms"] = latency_report["submit_ack_ms"]["p50"]
        summary["tick_p50_ms"] = latency_report["tick_ms"]["p50"]
        summary["scenario_total_p50_ms"] = latency_report["scenario_total_ms"]["p50"]
        summary["serve_roundtrip_p50_ms"] = latency_report["serve_roundtrip_ms"]["p50"]

    return {
        "schema_version": SCHEMA_VERSION,
        "benchmark_name": tasks["name"],
        "recorded_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git": _git_metadata(base_branch=base_branch),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "iterations": iterations,
        },
        "summary": summary,
        "suites": suite_reports,
        "latency": latency_report,
    }


def _regressed_cases_for_suite(base_res: Dict[str, Any], curr_res: Dict[str, Any], suite_name: str) -> List[str]:
    base_cases = {c["id"]: c.get("passed", False) for c in base_res.get("suites", {}).get(suite_name, {}).get("cases", [])}
    curr_cases = curr_res.get("suites", {}).get(suite_name, {}).get("cases", [])
    regressed = [c["id"] for c in curr_cases if not c.get("passed", False) and base_cases.get(c["id"], True)]
    if not regressed:
        regressed = [c["id"] for c in curr_cases if not c.get("passed", False)]
    return regressed


def compare_results(
    baseline: Dict[str, Any],
    current: Dict[str, Any],
    accuracy_tol: float = 0.0,
    latency_tol_pct: float = 50.0,
    latency_tol_ms: float = 50.0,
    baseline_path: str = "",
) -> Dict[str, Any]:
    base_sum = baseline.get("summary", {})
    curr_sum = current.get("summary", {})

    accuracy_metrics = [
        ("relation_accuracy", "relation"),
        ("invalidation_exact_match", "invalidation"),
        ("invalidation_f1", "invalidation"),
        ("merge_accuracy", "merge"),
        ("scenario_pass_rate", "scenarios"),
        ("overall_accuracy", None),
    ]
    latency_metrics = [
        "submit_ack_p50_ms",
        "tick_p50_ms",
        "scenario_total_p50_ms",
        "serve_roundtrip_p50_ms",
    ]

    deltas: Dict[str, Any] = {}
    regressions: List[Dict[str, Any]] = []

    for metric, suite_name in accuracy_metrics:
        if metric not in base_sum or metric not in curr_sum:
            continue
        b_val = float(base_sum[metric])
        c_val = float(curr_sum[metric])
        delta = round(c_val - b_val, 4)
        regressed = c_val < (b_val - accuracy_tol)
        deltas[metric] = {
            "baseline": b_val,
            "current": c_val,
            "delta": delta,
            "status": "regression" if regressed else ("improved" if delta > 0 else "ok"),
        }
        if regressed:
            bad_cases = _regressed_cases_for_suite(baseline, current, suite_name) if suite_name else []
            repro = (
                f"python3 scripts/benchmark.py --suite {suite_name} --case {bad_cases[0]}"
                if suite_name and bad_cases
                else (f"python3 scripts/benchmark.py --suite {suite_name}" if suite_name else "python3 scripts/benchmark.py")
            )
            regressions.append({
                "metric": metric,
                "kind": "accuracy",
                "baseline": b_val,
                "current": c_val,
                "delta": delta,
                "regressed_cases": bad_cases,
                "suspect_modules": SUSPECT_MODULES.get(metric, ["goa/agent.py"]),
                "reproduce": repro,
            })

    for metric in latency_metrics:
        if metric not in base_sum or metric not in curr_sum:
            continue
        b_val = float(base_sum[metric])
        c_val = float(curr_sum[metric])
        delta_ms = round(c_val - b_val, 3)
        pct = round((delta_ms / b_val) * 100.0, 2) if b_val > 0 else 0.0
        regressed = (delta_ms > latency_tol_ms) and (pct > latency_tol_pct)
        deltas[metric] = {
            "baseline_ms": b_val,
            "current_ms": c_val,
            "delta_ms": delta_ms,
            "delta_pct": pct,
            "status": "regression" if regressed else ("improved" if delta_ms < -latency_tol_ms else "ok"),
        }
        if regressed:
            regressions.append({
                "metric": metric,
                "kind": "latency",
                "baseline_ms": b_val,
                "current_ms": c_val,
                "delta_ms": delta_ms,
                "delta_pct": pct,
                "suspect_modules": SUSPECT_MODULES.get(metric, ["goa/agent.py"]),
                "reproduce": "python3 scripts/benchmark.py --suite scenarios --iterations 5",
            })

    return {
        "passed": not regressions,
        "baseline": {
            "path": baseline_path,
            "commit": baseline.get("git", {}).get("short_commit", ""),
            "recorded_at": baseline.get("recorded_at", ""),
        },
        "current": {
            "commit": current.get("git", {}).get("short_commit", ""),
            "recorded_at": current.get("recorded_at", ""),
        },
        "tolerances": {
            "accuracy_tol": accuracy_tol,
            "latency_tol_pct": latency_tol_pct,
            "latency_tol_ms": latency_tol_ms,
        },
        "deltas": deltas,
        "regressions": regressions,
    }


def _format_run_summary(report: Dict[str, Any]) -> str:
    s = report["summary"]
    g = report["git"]
    lines = [
        f"GOA Benchmark ({g.get('short_commit') or 'unknown'} @ {g.get('branch') or 'unknown'})",
        f"  overall_accuracy        : {s['overall_accuracy']:.4f} ({s['passed_cases']}/{s['total_cases']})",
    ]
    if "relation_accuracy" in s:
        r = report["suites"]["relation"]
        lines.append(f"  relation_accuracy       : {s['relation_accuracy']:.4f} ({r['passed']}/{r['total']})")
    if "invalidation_exact_match" in s:
        inv = report["suites"]["invalidation"]
        lines.append(
            f"  invalidation_exact_match: {s['invalidation_exact_match']:.4f} "
            f"(F1 {s['invalidation_f1']:.4f}, {inv['passed']}/{inv['total']})"
        )
    if "merge_accuracy" in s:
        m = report["suites"]["merge"]
        lines.append(f"  merge_accuracy          : {s['merge_accuracy']:.4f} ({m['passed']}/{m['total']})")
    if "scenario_pass_rate" in s:
        sc = report["suites"]["scenarios"]
        lines.append(f"  scenario_pass_rate      : {s['scenario_pass_rate']:.4f} ({sc['passed']}/{sc['total']})")
        lines.append(
            f"  latency p50 (ms)        : submit_ack={s['submit_ack_p50_ms']:.2f} "
            f"tick={s['tick_p50_ms']:.2f} scenario={s['scenario_total_p50_ms']:.2f} "
            f"serve={s['serve_roundtrip_p50_ms']:.2f}"
        )
    return "\n".join(lines)


def _format_comparison(cmp: Dict[str, Any]) -> str:
    status = "PASS" if cmp["passed"] else "REGRESSION"
    lines = [
        f"Benchmark Comparison: {status} "
        f"(baseline {cmp['baseline'].get('commit') or '?'} -> current {cmp['current'].get('commit') or '?'})"
    ]
    for metric, info in cmp["deltas"].items():
        if "baseline_ms" in info:
            lines.append(
                f"  {metric:<24}: {info['baseline_ms']:.2f}ms -> {info['current_ms']:.2f}ms "
                f"({info['delta_ms']:+.2f}ms / {info['delta_pct']:+.1f}%) [{info['status']}]"
            )
        else:
            lines.append(
                f"  {metric:<24}: {info['baseline']:.4f} -> {info['current']:.4f} "
                f"({info['delta']:+.4f}) [{info['status']}]"
            )
    if cmp["regressions"]:
        lines.append("Regressions detected:")
        for reg in cmp["regressions"]:
            cases_note = f" cases={reg['regressed_cases']}" if reg.get("regressed_cases") else ""
            lines.append(f"  - {reg['metric']}{cases_note}")
            lines.append(f"    suspect_modules: {', '.join(reg['suspect_modules'])}")
            lines.append(f"    reproduce      : {reg['reproduce']}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run headless git-ontology-agent core benchmarks and compare against baseline results."
    )
    parser.add_argument("--tasks", type=Path, default=DEFAULT_TASKS_PATH, help="path to fixed benchmark tasks JSON")
    parser.add_argument(
        "--suite",
        choices=("all", *ALL_SUITES),
        default="all",
        help="benchmark suite to run (default: all)",
    )
    parser.add_argument("--case", default=None, help="run only a single case id within the selected suite(s)")
    parser.add_argument("--iterations", type=int, default=3, help="measured iterations per latency scenario (default: 3)")
    parser.add_argument("--output", type=Path, default=None, help="write full benchmark result JSON to this path")
    parser.add_argument("--json", action="store_true", help="print JSON output to stdout")
    parser.add_argument(
        "--write-baseline",
        nargs="?",
        const=str(DEFAULT_BASELINE_PATH),
        default=None,
        metavar="PATH",
        help="record benchmark result as baseline JSON (default: benchmarks/baseline.json)",
    )
    parser.add_argument(
        "--compare",
        nargs="?",
        const=str(DEFAULT_BASELINE_PATH),
        default=None,
        metavar="BASELINE_JSON",
        help="run benchmark and compare against a baseline/previous result JSON",
    )
    parser.add_argument(
        "--compare-files",
        nargs=2,
        metavar=("BASELINE_JSON", "CURRENT_JSON"),
        default=None,
        help="compare two existing JSON result files without running benchmarks",
    )
    parser.add_argument("--accuracy-tol", type=float, default=0.0, help="allowed accuracy drop before failing (default: 0.0)")
    parser.add_argument(
        "--latency-tol-pct",
        type=float,
        default=50.0,
        help="allowed relative p50 latency increase in percent (default: 50.0)",
    )
    parser.add_argument(
        "--latency-tol-ms",
        type=float,
        default=50.0,
        help="allowed absolute p50 latency increase in ms (default: 50.0)",
    )
    args = parser.parse_args(argv)

    if args.compare_files:
        base_path, curr_path = Path(args.compare_files[0]), Path(args.compare_files[1])
        baseline = json.loads(base_path.read_text(encoding="utf-8"))
        current = json.loads(curr_path.read_text(encoding="utf-8"))
        cmp = compare_results(
            baseline,
            current,
            accuracy_tol=args.accuracy_tol,
            latency_tol_pct=args.latency_tol_pct,
            latency_tol_ms=args.latency_tol_ms,
            baseline_path=str(base_path),
        )
        print(json.dumps(cmp, indent=2, ensure_ascii=False) if args.json else _format_comparison(cmp))
        return 0 if cmp["passed"] else 1

    suites = ALL_SUITES if args.suite == "all" else (args.suite,)
    report = run_benchmark(
        tasks_path=args.tasks,
        suites=suites,
        iterations=args.iterations,
        case_filter=args.case,
    )

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if args.write_baseline:
        out_path = Path(args.write_baseline)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if args.compare:
        base_path = Path(args.compare)
        baseline = json.loads(base_path.read_text(encoding="utf-8"))
        cmp = compare_results(
            baseline,
            report,
            accuracy_tol=args.accuracy_tol,
            latency_tol_pct=args.latency_tol_pct,
            latency_tol_ms=args.latency_tol_ms,
            baseline_path=str(base_path),
        )
        if args.json:
            print(json.dumps({"result": report, "comparison": cmp}, indent=2, ensure_ascii=False))
        else:
            print(_format_run_summary(report))
            print()
            print(_format_comparison(cmp))
        return 0 if cmp["passed"] else 1

    print(json.dumps(report, indent=2, ensure_ascii=False) if args.json else _format_run_summary(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
