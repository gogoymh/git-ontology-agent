"""Reproduces the gaps GOA-31 found while grading GOA-18 (judge evidence; not a unittest).

    python3 tests/goa31_gaps_check.py record-conflict|pair-commit|noisy-baseline|harness-leak

Each of those modes exits 0 while the gap is still there and 1 once it is fixed.
`tickets` and `overlap` exit 0 when the follow-up board matches this grade.
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def gap(ok, msg):
    print(("gap " if ok else "fixed ") + msg)
    sys.exit(0 if ok else 1)


def _run():
    return json.loads((ROOT / "benchmarks" / "runs" / "a0d96a3.json").read_text(encoding="utf-8"))


def _watch():
    return (ROOT / "scripts" / "bench_watch.py").read_text(encoding="utf-8")


def record_conflict():
    # The saved comparison still lists latency flags the side-by-side run dismissed.
    run = _run()
    src = _watch()
    json_bad = (
        run["passed"] is True
        and run["comparison"]["passed"] is False
        and run["regressions"] == []
        and any(r.get("kind") == "latency" for r in run["comparison"]["regressions"])
    )
    code_bad = '"comparison": cmp' in src and 'cmp["passed"]' not in src and "cmp['passed']" not in src
    gap(json_bad or code_bad,
        "record-conflict: comparison.passed is false while passed is true; scripts/bench_watch.py:269 stores cmp")


def pair_commit():
    # The first run remeasured 33deb7d, not the baseline's commit 52faeea.
    base = json.loads((ROOT / "benchmarks" / "baseline.json").read_text(encoding="utf-8"))
    prev = _run()["previous_commit"]
    short = base["git"]["short_commit"]
    gap(not prev.startswith(short),
        "pair-commit: previous_commit %s != baseline %s (benchmarks/runs/a0d96a3.json:4)" % (prev[:7], short))


def noisy_baseline():
    # last.json keeps the first wall-clock sample, not the paired best.
    src = _watch()
    idx = src.find("last_path.write_text")
    snippet = src[idx:idx + 80]
    before = src[max(0, idx - 400):idx]
    gap("json.dumps(result" in snippet and "best[" not in before,
        "noisy-baseline: scripts/bench_watch.py:279 stores the first harness result, not the paired best")


def harness_leak():
    # load_compare imports goa from a temp tree and leaves it loaded after the tree is deleted.
    sys.path.insert(0, str(ROOT / "scripts"))
    import bench_watch  # noqa: E402

    if "goa" in sys.modules:
        gap(False, "harness-leak: goa was already imported, so this process cannot show the leak")
    tmp = Path(tempfile.mkdtemp(prefix="goa-bench."))
    try:
        tree = tmp / "current"
        bench_watch.export_tree(ROOT, "HEAD", tree)
        bench_watch.load_compare(tree)
        goa_file = Path(os.path.realpath(sys.modules["goa"].__file__))
        leaked = str(os.path.realpath(tmp)) in str(goa_file)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    cli_fails = False
    try:
        import goa.cli  # noqa: F401
    except ModuleNotFoundError:
        cli_fails = True
    gap(leaked and not goa_file.exists() and "goa" in sys.modules and cli_fails,
        "harness-leak: goa stayed loaded from a deleted tree (scripts/bench_watch.py:84) and goa.cli cannot import")


def _hub(*parts):
    home = Path.home() / ".preconquer" / "hub"
    hits = sorted(home.glob("git-ontology-agent-*/" + "/".join(parts)))
    if not hits:
        sys.exit("no git-ontology-agent hub at %s" % home)
    return hits[0]


def _issues():
    folder = _hub("issues")
    found = []
    for path in folder.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        data["_n"] = int(path.stem)
        found.append(data)
    return found


def _doc():
    return _hub("docs", "GOA-18 채점.md").read_text(encoding="utf-8")


def tickets():
    # Each new gap has one to-do issue, and the grade document names that issue.
    doc = _doc()
    issues = _issues()
    need = (
        "scripts/bench_watch.py:269",
        "benchmarks/runs/a0d96a3.json:4",
        "scripts/bench_watch.py:84",
    )
    for key in need:
        hits = [i for i in issues if i.get("status") == "todo" and key in (i.get("body") or "") and "할 일" in (i.get("body") or "")]
        if len(hits) != 1:
            sys.exit("tickets: %s is in %d to-do issues, want 1" % (key, len(hits)))
        name = "GOA-%d" % hits[0]["_n"]
        if name not in doc:
            sys.exit("tickets: %s is not in the grade document" % name)
    print("ok tickets")


def overlap():
    # GOA-28 and GOA-30 stay in to do, with this grade's notes, and are not re-filed.
    by_n = {i["_n"]: i for i in _issues()}
    i28, i30 = by_n.get(28), by_n.get(30)
    if not i28 or not i30:
        sys.exit("overlap: GOA-28 or GOA-30 is missing")
    ok = (
        i28.get("status") == "todo"
        and i30.get("status") == "todo"
        and "tests/test_benchmark.py:87" in (i28.get("body") or "")
        and "GOA-18" in (i28.get("body") or "")
        and "test_bench_watch" in (i28.get("body") or "")
        and "scripts/bench_watch.py:279" in (i30.get("body") or "")
        and "315.471" in (i30.get("body") or "")
        and "noisy-baseline" in (i30.get("body") or "")
    )
    if not ok:
        sys.exit("overlap: GOA-28 or GOA-30 was not updated")
    print("ok overlap")


MODES = {
    "record-conflict": record_conflict,
    "pair-commit": pair_commit,
    "noisy-baseline": noisy_baseline,
    "harness-leak": harness_leak,
    "tickets": tickets,
    "overlap": overlap,
}


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in MODES:
        sys.exit("usage: python3 tests/goa31_gaps_check.py " + "|".join(MODES))
    MODES[sys.argv[1]]()
