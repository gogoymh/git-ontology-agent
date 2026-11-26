"""Reproduces the gaps GOA-22 found while grading GOA-11/14/15/17/20 (judge evidence; not a unittest).

    python3 tests/goa22_gaps_check.py engine-stop|history-scan|bench-tests|deck-dep|ci-comment

Each mode exits 0 while the gap is still there and 1 once it is fixed, so a follow-up ticket
can flip it to prove the fix.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def gap(ok, msg):
    print(("gap " if ok else "fixed ") + msg)
    sys.exit(0 if ok else 1)


def engine_stop():
    # GOA-14: stop() waits on `done`, which only resolves on exit; SIGTERM is the only escalation.
    src = (ROOT / "ui/src/engine.ts").read_text(encoding="utf-8")
    gap("SIGTERM" in src and "SIGKILL" not in src and "return done;" in src,
        "engine-stop: ui/src/engine.ts stop() has no SIGKILL fallback or timeout, so /connect can wait forever")


def history_scan():
    # GOA-17: history(limit=1) still reads the state of every cancelled/req/* branch.
    sys.path.insert(0, str(ROOT))
    os.environ.update(GOA_ENV_FILE=os.devnull, GOA_LOG="error")
    from goa import Agent
    d = tempfile.mkdtemp(prefix="goa22-hist-")
    try:
        repo = os.path.join(d, "conv")
        a = Agent(repo, lang="en")
        a.submit("Plan my Busan business trip next week")
        a.run()
        for i in range(40):
            subprocess.run(["git", "-C", repo, "branch", f"cancelled/req/{100 + i}", "main"], check=True)
        b = Agent(repo, lang="en")
        calls = []
        real = b.repo.read_state
        b.repo.read_state = lambda ref: (calls.append(ref), real(ref))[1]
        b.history(limit=1)
        n = sum(1 for r in calls if r.startswith("cancelled/"))
    finally:
        shutil.rmtree(d, True)
    gap(n >= 40, f"history-scan: history(limit=1) read {n} cancelled branch states (goa/agent.py:165)")


def bench_tests():
    # GOA-15: unit tests run the whole 55-case benchmark (iterations 1 and 2) inside `unittest discover`.
    src = (ROOT / "tests/test_benchmark.py").read_text(encoding="utf-8")
    full = [m.group(0) for m in re.finditer(r"_run_cli\(\s*\"--(iterations|compare)\"", src)]
    gap(len(full) >= 2, f"bench-tests: {len(full)} full benchmark runs in tests/test_benchmark.py (lines 87, 156)")


def deck_dep():
    # GOA-11: README tells users to run scripts/make_deck_ko.js, which requires an undeclared package.
    script = (ROOT / "scripts/make_deck_ko.js").read_text(encoding="utf-8")
    declared = any("pptxgenjs" in p.read_text(encoding="utf-8")
                   for p in ROOT.glob("**/package.json") if "node_modules" not in p.parts)
    gap('require("pptxgenjs")' in script and not declared,
        "deck-dep: pptxgenjs is required by scripts/make_deck_ko.js:5 but no package.json declares it")


def ci_comment():
    # GOA-20: tests/ci_check.py does a substring match, so a commented-out step still passes.
    d = Path(tempfile.mkdtemp(prefix="goa22-ci-"))
    try:
        (d / "tests").mkdir()
        (d / ".github/workflows").mkdir(parents=True)
        shutil.copy(ROOT / "tests/ci_check.py", d / "tests/ci_check.py")
        ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        ci = re.sub(r"^(\s*)- run: (python tests/(readme|deck)_)", r"\1# - run: \2", ci, flags=re.M)
        (d / ".github/workflows/ci.yml").write_text(ci, encoding="utf-8")
        r = subprocess.run([sys.executable, str(d / "tests/ci_check.py")], capture_output=True, text=True)
    finally:
        shutil.rmtree(d, True)
    gap(r.returncode == 0, "ci-comment: tests/ci_check.py:12 passes with every doc step commented out")


if __name__ == "__main__":
    {"engine-stop": engine_stop, "history-scan": history_scan, "bench-tests": bench_tests,
     "deck-dep": deck_dep, "ci-comment": ci_comment}[sys.argv[1]]()
