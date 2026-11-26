"""Reproduces the gap GOA-37 found while grading GOA-28 and GOA-35 (judge evidence; not a unittest).

    python3 tests/goa37_gaps_check.py suite-shape

suite-shape exits 0 while the gap is still there and 1 once it is fixed.
`tickets`, `overlap` and `grade` exit 0 when the board and the grade document match this grade.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = "GOA-28과 GOA-35 채점.md"
KEY = "tests/test_benchmark.py:109"


def gap(ok, msg):
    print(("gap " if ok else "fixed ") + msg)
    sys.exit(0 if ok else 1)


def suite_shape():
    # GOA-28 cut the output-shape checks: no unit test runs the invalidation suite or a serve case.
    src = "\n".join((ROOT / "tests" / n).read_text(encoding="utf-8")
                    for n in ("test_benchmark.py", "test_bench_watch.py"))
    runs_inv = bool(re.search(r'"--suite",\s*"(invalidation|all)"', src))
    runs_serve = "e2e_serve" in src
    weak_serve = 'assertGreaterEqual(summary["serve_roundtrip_p50_ms"], 0.0)' in src
    gap(not runs_inv or not runs_serve or weak_serve,
        "suite-shape: invalidation suite run=%s, serve case run=%s, serve latency only >= 0=%s "
        "(tests/test_benchmark.py:109, :120, :122)" % (runs_inv, runs_serve, weak_serve))


def _hub(*parts):
    home = Path.home() / ".preconquer" / "hub"
    hits = sorted(home.glob("git-ontology-agent-*/" + "/".join(parts)))
    if not hits:
        sys.exit("no git-ontology-agent hub file %s under %s" % ("/".join(parts), home))
    return hits[0]


def _issues():
    found = {}
    for path in _hub("issues").glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        found[int(path.stem)] = data
    return found


def _doc():
    return _hub("docs", DOC).read_text(encoding="utf-8")


def tickets():
    # The gap has exactly one to-do issue with evidence and steps, and the grade document names it.
    hits = [n for n, i in _issues().items()
            if i.get("status") == "todo" and KEY in (i.get("body") or "") and "할 일" in (i.get("body") or "")]
    if len(hits) != 1:
        sys.exit("tickets: %s is in %d to-do issues, want 1" % (KEY, len(hits)))
    if "GOA-%d" % hits[0] not in _doc():
        sys.exit("tickets: GOA-%d is not in the grade document" % hits[0])
    print("ok tickets GOA-%d" % hits[0])


def overlap():
    # GOA-28 and GOA-35 are done; GOA-30 and GOA-36 stay in to do with this grade's notes.
    by_n = _issues()
    need = {28: "done", 35: "done", 30: "todo", 36: "todo"}
    for n, status in need.items():
        if n not in by_n or by_n[n].get("status") != status:
            sys.exit("overlap: GOA-%d should be %s" % (n, status))
    i36 = by_n[36].get("body") or ""
    if ".github/workflows/ci.yml:14" not in i36 or "GOA-37" not in i36:
        sys.exit("overlap: GOA-36 lacks the CI line or the GOA-37 note")
    notes30 = " ".join(c.get("text", "") if isinstance(c, dict) else str(c)
                       for c in by_n[30].get("comments") or [])
    if "GOA-37" not in (by_n[30].get("body") or "") + notes30:
        sys.exit("overlap: GOA-30 lacks the GOA-37 note")
    print("ok overlap")


def grade():
    # The grade document scores both missions out of 100 and records the duplicate work.
    doc = _doc()
    for name in ("GOA-28", "GOA-35"):
        if not re.search(name + r"[^\n]*\b\d{1,3}\s*/\s*100", doc):
            sys.exit("grade: no score line for %s" % name)
    for word in ("중복", "GOA-30", "GOA-36"):
        if word not in doc:
            sys.exit("grade: document does not mention %s" % word)
    print("ok grade")


MODES = {"suite-shape": suite_shape, "tickets": tickets, "overlap": overlap, "grade": grade}


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in MODES:
        sys.exit("usage: python3 tests/goa37_gaps_check.py " + "|".join(MODES))
    MODES[sys.argv[1]]()
