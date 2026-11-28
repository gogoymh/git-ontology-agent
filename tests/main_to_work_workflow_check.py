"""Checks that the Preconquer workflow 'Main into preconquer' is registered on this machine (mission criterion; not a unittest).

    python3 tests/main_to_work_workflow_check.py

It must run often (every 30 minutes or less), run scripts/main_to_work.sh from preconquer-gogoymh,
and skip cleanly while that branch lacks the script.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

home = Path(os.environ.get("PRECONQUER_REAL_HOME") or Path.home() / ".preconquer")
found = [json.loads(p.read_text(encoding="utf-8")) for p in home.glob("workflows/git-ontology-agent-*/*.json")]
wf = next((w for w in found if isinstance(w, dict) and w.get("name") == "Main into preconquer"), None)
if not wf:
    sys.exit("main_to_work_workflow_check: no 'Main into preconquer' workflow under %s" % home)

routine = wf.get("routine") or {}
if routine.get("kind") != "every" or not 0 < int(routine.get("minutes", 0)) <= 30:
    sys.exit("main_to_work_workflow_check: routine must run every 30 minutes or less, got %r" % routine)

steps = wf.get("steps") or []
cmd = (steps[0] if steps else {}).get("command", "")
if steps[0].get("kind") != "shell" or "preconquer-gogoymh:scripts/main_to_work.sh" not in cmd \
        or "WORK_BRANCH=preconquer-gogoymh" not in cmd:
    sys.exit("main_to_work_workflow_check: first step must run main_to_work.sh from preconquer-gogoymh")

with tempfile.TemporaryDirectory(prefix="goa-m2w-wf.") as d:
    for args in (["init", "-q", "-b", "preconquer-gogoymh"], ["-c", "user.email=a@b.invalid", "-c", "user.name=a",
                                                              "commit", "-q", "--allow-empty", "-m", "x"]):
        subprocess.run(["git", *args], cwd=d, check=True)
    res = subprocess.run(["/bin/sh", "-c", cmd], cwd=d, capture_output=True, text=True)
    if res.returncode != 0 or "skipping" not in res.stdout:
        sys.exit("main_to_work_workflow_check: step 1 should skip cleanly before the merge: %r" % (res.stdout + res.stderr))

print("ok workflow %s: every %s min, %d step(s)" % (wf.get("id"), routine["minutes"], len(steps)))
