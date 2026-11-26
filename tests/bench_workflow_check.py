"""Checks that the Preconquer workflow 'Benchmark on merge' is registered on this machine (mission criterion; not a unittest).

    python3 tests/bench_workflow_check.py

It must run often (every 30 minutes or less), run scripts/bench_watch.py from preconquer-gogoymh,
skip cleanly while that branch lacks the script, and hand a regression report to an agent step
that files an issue only when regression.md exists.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

home = Path(os.environ.get("PRECONQUER_REAL_HOME") or Path.home() / ".preconquer")
found = [json.loads(p.read_text(encoding="utf-8")) for p in home.glob("workflows/git-ontology-agent-*/*.json")]
wf = next((w for w in found if isinstance(w, dict) and w.get("name") == "Benchmark on merge"), None)
if not wf:
    sys.exit("bench_workflow_check: no 'Benchmark on merge' workflow under %s" % home)

routine = wf.get("routine") or {}
if routine.get("kind") != "every" or not 0 < int(routine.get("minutes", 0)) <= 30:
    sys.exit("bench_workflow_check: routine must run every 30 minutes or less, got %r" % routine)

steps = wf.get("steps") or []
shell = steps[0] if steps else {}
cmd = shell.get("command", "")
if shell.get("kind") != "shell" or "preconquer-gogoymh:scripts/bench_watch.py" not in cmd or "--ref preconquer-gogoymh" not in cmd:
    sys.exit("bench_workflow_check: first step must run bench_watch.py from preconquer-gogoymh")

agent = next((s for s in steps[1:] if s.get("kind") == "agent"), {})
if "goa-bench/regression.md" not in agent.get("unless", "") or "issue_create" not in agent.get("prompt", ""):
    sys.exit("bench_workflow_check: an agent step must file an issue only when regression.md exists")

# The first step must succeed (and do nothing) in a repository whose branch lacks the script yet.
with tempfile.TemporaryDirectory(prefix="goa-bench-wf.") as d:
    for args in (["init", "-q", "-b", "preconquer-gogoymh"], ["-c", "user.email=a@b.invalid", "-c", "user.name=a",
                                                              "commit", "-q", "--allow-empty", "-m", "x"]):
        subprocess.run(["git", *args], cwd=d, check=True)
    res = subprocess.run(["/bin/sh", "-c", cmd], cwd=d, capture_output=True, text=True)
    if res.returncode != 0 or "skipping" not in res.stdout:
        sys.exit("bench_workflow_check: step 1 should skip cleanly before the merge: %r" % (res.stdout + res.stderr))
    # With no regression.md the agent step is skipped (its unless command exits 0).
    if subprocess.run(["/bin/sh", "-c", agent["unless"]], cwd=d).returncode != 0:
        sys.exit("bench_workflow_check: the agent step would start without a regression report")

print("ok workflow %s: every %s min, %d steps" % (wf.get("id"), routine["minutes"], len(steps)))
