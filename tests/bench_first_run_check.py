"""Checks the recorded first bench_watch run over the GOA-14 and GOA-17 merges (mission criterion; not a unittest).

    python3 tests/bench_first_run_check.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
path = ROOT / "benchmarks" / "runs" / "a0d96a3.json"
if not path.exists():
    sys.exit("bench_first_run_check: %s is missing" % path.relative_to(ROOT))
rec = json.loads(path.read_text(encoding="utf-8"))

assert rec["commit"].startswith("a0d96a3"), rec["commit"]
assert rec["previous_source"].endswith("benchmarks/baseline.json"), "first run must compare with the baseline"
subjects = " ".join(rec.get("merges") or [])
for ticket in ("GOA-14", "GOA-17"):
    assert ticket in subjects, "%s merge is not in the compared range: %s" % (ticket, subjects)
deltas = rec["comparison"]["deltas"]
for metric in ("overall_accuracy", "relation_accuracy", "merge_accuracy", "scenario_pass_rate",
               "submit_ack_p50_ms", "serve_roundtrip_p50_ms"):
    assert metric in deltas, "no comparison for %s" % metric
assert isinstance(rec["passed"], bool) and rec["passed"] == (not rec["regressions"]), "regression verdict missing"
print("ok first run a0d96a3: %s, accuracy %.4f, %d metrics compared" % (
    "no regression" if rec["passed"] else "regression in " + ", ".join(r["metric"] for r in rec["regressions"]),
    rec["summary"]["overall_accuracy"], len(deltas)))
