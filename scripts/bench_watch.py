#!/usr/bin/env python3
"""Benchmark every new merge on a branch and compare it with the previous result.

Meant to run from a Preconquer workflow routine in the project folder, whatever branch
that folder has checked out:

  git show preconquer-gogoymh:scripts/bench_watch.py | python3 - --ref preconquer-gogoymh

What one run does:
  1. Resolves --ref. If its commit was already benchmarked, exits 0 right away.
  2. Exports that commit's tree with `git archive` into a temp folder (the working tree,
     the index and the worktree list stay untouched) and runs scripts/benchmark.py there
     headless: network blocked, mock models only, no window, no focus.
  3. Compares the scores with the previous result (the last run, or the commit's
     benchmarks/baseline.json on the first run).
  4. A latency regression is re-measured side by side: the previous commit's tree and the
     current one run the same harness alternately, and only a slowdown that persists counts.
     This keeps machine load from filing false regressions.
  5. Records runs/<commit>.json and history.jsonl in the state folder
     (<git common dir>/goa-bench by default), and on a regression writes regression.md
     with the metrics, suspect modules, reproduce commands and the merges in range.

Exit status: 0 when the run finished (regression or not; regression.md is the signal),
1 on a regression with --fail-on-regression, 2 when the benchmark itself could not run.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

LATENCY_METRICS = ("submit_ack_p50_ms", "tick_p50_ms", "scenario_total_p50_ms", "serve_roundtrip_p50_ms")
HARNESS = ("scripts/benchmark.py", "benchmarks/tasks.json")


def log(msg: str) -> None:
    print(f"[bench-watch] {msg}", flush=True)


def git(*args: str, cwd: Optional[Path] = None, check: bool = True) -> str:
    res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {res.stderr.strip()}")
    return res.stdout.strip() if res.returncode == 0 else ""


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def export_tree(repo: Path, commit: str, dest: Path) -> None:
    """Write the commit's files into dest without touching the repository's checkouts."""
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.Popen(["git", "archive", "--format=tar", commit], cwd=repo, stdout=subprocess.PIPE)
    untar = subprocess.run(["tar", "-x", "-C", str(dest)], stdin=archive.stdout, capture_output=True)
    archive.stdout.close()  # type: ignore[union-attr]
    if archive.wait() != 0 or untar.returncode != 0:
        raise RuntimeError(f"could not export {commit[:7]}: {untar.stderr.decode().strip()}")


def run_harness(tree: Path, out: Path, suite: str, iterations: int) -> Dict[str, Any]:
    """Run scripts/benchmark.py inside an exported tree and return its JSON result."""
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", NO_COLOR="1")
    cmd = [sys.executable, "scripts/benchmark.py", "--suite", suite,
           "--iterations", str(iterations), "--output", str(out)]
    res = subprocess.run(cmd, cwd=tree, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if res.returncode != 0 or not out.exists():
        tail = (res.stderr or res.stdout).strip().splitlines()[-5:]
        raise RuntimeError(f"benchmark failed in {tree.name} (exit {res.returncode}): {' | '.join(tail)}")
    return json.loads(out.read_text(encoding="utf-8"))


def load_compare(tree: Path) -> Callable[..., Dict[str, Any]]:
    """Use compare_results from the harness of the commit under test."""
    import importlib.util
    sys.path.insert(0, str(tree))
    try:
        spec = importlib.util.spec_from_file_location("goa_bench_harness", tree / "scripts" / "benchmark.py")
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod.compare_results
    finally:
        sys.path.remove(str(tree))


def paired_latency(
    measure: Callable[[str, int], Dict[str, float]],
    rounds: int,
) -> Dict[str, Dict[str, float]]:
    """Measure 'previous' and 'current' alternately and keep the best p50 of each side.

    The best of several runs is the least disturbed by other load on the machine, and
    alternating spreads any load spike over both sides.
    """
    best: Dict[str, Dict[str, float]] = {"previous": {}, "current": {}}
    for i in range(max(1, rounds)):
        for side in ("previous", "current"):
            for metric, value in measure(side, i).items():
                if metric in LATENCY_METRICS:
                    prev = best[side].get(metric)
                    best[side][metric] = value if prev is None else min(prev, value)
    return best


def merges_between(repo: Path, old: str, new: str) -> List[str]:
    if not old:
        return []
    out = git("log", "--merges", "--format=%h %s", f"{old}..{new}", cwd=repo, check=False)
    return [line for line in out.splitlines() if line]


def regression_markdown(record: Dict[str, Any]) -> str:
    cur, prev = record["commit"][:7], (record.get("previous_commit") or "?")[:7]
    lines = [f"Benchmark regression on {record['ref']} at {cur} (previous result {prev})", ""]
    lines.append("Regressed metrics:")
    for reg in record["regressions"]:
        if reg["kind"] == "accuracy":
            cases = f", cases {', '.join(reg['regressed_cases'])}" if reg.get("regressed_cases") else ""
            lines.append(f"- {reg['metric']}: {reg['baseline']:.4f} -> {reg['current']:.4f}{cases}")
        else:
            lines.append(
                f"- {reg['metric']}: {reg['baseline_ms']:.1f}ms -> {reg['current_ms']:.1f}ms "
                f"({reg['delta_pct']:+.1f}%, confirmed side by side)"
            )
    lines += ["", "Suspect modules:"]
    suspects: List[str] = []
    for reg in record["regressions"]:
        suspects += [m for m in reg.get("suspect_modules", []) if m not in suspects]
    lines += [f"- {m}" for m in suspects]
    lines += ["", "Merges since the previous result:"]
    lines += [f"- {m}" for m in record.get("merges") or ["(unknown range)"]]
    lines += ["", "Reproduce (checkout of the regressed commit):"]
    repros: List[str] = []
    for reg in record["regressions"]:
        if reg.get("reproduce") and reg["reproduce"] not in repros:
            repros.append(reg["reproduce"])
    lines += [f"- {r}" for r in repros]
    if record.get("previous_commit"):
        lines.append(f"- git diff {prev}..{cur} -- {' '.join(suspects)}")
    return "\n".join(lines) + "\n"


@contextlib.contextmanager
def single_run(state: Path):
    """Skip quietly when another run still holds the lock (routines can overlap)."""
    import fcntl
    state.mkdir(parents=True, exist_ok=True)
    fh = open(state / "lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        yield False
        return
    try:
        yield True
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Benchmark each new merge on a branch and compare with the previous result.")
    p.add_argument("--ref", default="preconquer-gogoymh", help="branch or commit to watch (default: preconquer-gogoymh)")
    p.add_argument("--repo", type=Path, default=Path.cwd(), help="any folder inside the repository (default: cwd)")
    p.add_argument("--state-dir", type=Path, default=None, help="where results live (default: <git common dir>/goa-bench)")
    p.add_argument("--previous", type=Path, default=None, help="compare with this result JSON instead of the last run")
    p.add_argument("--pair-with", default=None, help="commit to re-measure latency against (default: the previous result's)")
    p.add_argument("--suite", default="all", help="benchmark suite to run (default: all)")
    p.add_argument("--iterations", type=int, default=3, help="latency iterations per scenario (default: 3)")
    p.add_argument("--rounds", type=int, default=2, help="alternating rounds for a side-by-side latency check (default: 2)")
    p.add_argument("--force", action="store_true", help="benchmark even if this commit already has a result")
    p.add_argument("--fail-on-regression", action="store_true", help="exit 1 when a regression is found")
    args = p.parse_args(argv)

    repo = Path(git("rev-parse", "--show-toplevel", cwd=args.repo))
    common = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=repo))
    state = (args.state_dir or Path(os.environ.get("GOA_BENCH_STATE") or common / "goa-bench")).resolve()

    with single_run(state) as got:
        if not got:
            log(f"another run holds {state / 'lock'}; skipping")
            return 0
        (state / "regression.md").unlink(missing_ok=True)  # a stale one must not file a second issue

        commit = git("rev-parse", "--verify", f"{args.ref}^{{commit}}", cwd=repo)
        last_path = state / "last.json"
        last = json.loads(last_path.read_text(encoding="utf-8")) if last_path.exists() else None
        if last and last.get("git", {}).get("commit") == commit and not args.force:
            log(f"no new merge on {args.ref} since {commit[:7]}; nothing to run")
            return 0

        if args.previous:
            previous, prev_source = json.loads(args.previous.read_text(encoding="utf-8")), str(args.previous)
        elif last:
            previous, prev_source = last, str(last_path)
        else:
            raw = git("show", f"{commit}:benchmarks/baseline.json", cwd=repo, check=False)
            if not raw:
                log(f"{commit[:7]} has no benchmarks/baseline.json and there is no previous run")
                return 2
            previous, prev_source = json.loads(raw), f"{commit[:7]}:benchmarks/baseline.json"
        prev_commit = args.pair_with or previous.get("git", {}).get("commit", "")
        if prev_commit:
            prev_commit = git("rev-parse", "--verify", f"{prev_commit}^{{commit}}", cwd=repo, check=False)
        log(f"benchmarking {args.ref} at {commit[:7]} against {prev_source}")

        tmp = Path(tempfile.mkdtemp(prefix="goa-bench."))
        try:
            cur_tree = tmp / "current"
            try:
                export_tree(repo, commit, cur_tree)
                result = run_harness(cur_tree, tmp / "current.json", args.suite, args.iterations)
            except RuntimeError as exc:
                log(str(exc))
                return 2
            result["git"] = {"commit": commit, "short_commit": commit[:7], "branch": args.ref,
                             "base_branch": args.ref, "base_commit": commit, "dirty": False}
            compare = load_compare(cur_tree)
            cmp = compare(previous, result, baseline_path=prev_source)
            regressions = [r for r in cmp["regressions"] if r["kind"] == "accuracy"]
            flagged = [r["metric"] for r in cmp["regressions"] if r["kind"] == "latency"]
            paired: Optional[Dict[str, Any]] = None

            if flagged and prev_commit:
                log(f"latency flagged ({', '.join(flagged)}); re-measuring {prev_commit[:7]} side by side")
                prev_tree = tmp / "previous"
                export_tree(repo, prev_commit, prev_tree)
                for rel in HARNESS:  # same harness on both sides so only the code differs
                    (prev_tree / rel).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(cur_tree / rel, prev_tree / rel)
                trees = {"previous": prev_tree, "current": cur_tree}

                def measure(side: str, i: int) -> Dict[str, float]:
                    out = tmp / f"{side}-{i}.json"
                    return run_harness(trees[side], out, "scenarios", args.iterations)["summary"]

                best = paired_latency(measure, args.rounds)
                pcmp = compare(
                    {"summary": best["previous"], "git": {"short_commit": prev_commit[:7]}},
                    {"summary": best["current"], "git": {"short_commit": commit[:7]}},
                    baseline_path=f"{prev_commit[:7]} re-measured",
                )
                confirmed = [r for r in pcmp["regressions"] if r["kind"] == "latency" and r["metric"] in flagged]
                regressions += confirmed
                paired = {"previous_commit": prev_commit, "rounds": args.rounds, "best_p50_ms": best,
                          "flagged": flagged, "confirmed": [r["metric"] for r in confirmed]}
                log(f"side by side: confirmed {paired['confirmed'] or 'none'} of {flagged}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        record = {
            "ref": args.ref,
            "commit": commit,
            "recorded_at": now(),
            "previous_commit": prev_commit,
            "previous_source": prev_source,
            "merges": merges_between(repo, prev_commit, commit),
            "passed": not regressions,
            "summary": result["summary"],
            "comparison": cmp,
            "paired_latency": paired,
            "regressions": regressions,
        }
        (state / "runs").mkdir(parents=True, exist_ok=True)
        (state / "runs" / f"{commit[:7]}.json").write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        with open(state / "history.jsonl", "a", encoding="utf-8") as fh:
            line = {k: record[k] for k in ("ref", "commit", "recorded_at", "previous_commit", "passed")}
            line["regressed"] = [r["metric"] for r in regressions]
            fh.write(json.dumps(line) + "\n")
        last_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        s = result["summary"]
        log(f"{commit[:7]}: accuracy {s['overall_accuracy']:.4f} ({s['passed_cases']}/{s['total_cases']}), "
            f"{'no regression' if not regressions else 'REGRESSION in ' + ', '.join(r['metric'] for r in regressions)}")
        if regressions:
            (state / "regression.md").write_text(regression_markdown(record), encoding="utf-8")
            log(f"wrote {state / 'regression.md'}")
            return 1 if args.fail_on_regression else 0
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
