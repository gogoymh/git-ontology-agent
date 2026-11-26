"""Tests for scripts/bench_watch.py: benchmark each new merge and compare with the previous result.

Each test builds its own throwaway git repository and state folder, so tests can run in parallel
and never touch this checkout. Run one group with, for example:

    python3 -m unittest tests.test_bench_watch.NewMergeTest
"""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import bench_watch  # noqa: E402


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def init_repo(path: Path) -> None:
    sh(path, "git", "init", "-q", "-b", "preconquer-gogoymh")
    sh(path, "git", "config", "user.email", "bench@example.invalid")
    sh(path, "git", "config", "user.name", "bench")
    sh(path, "git", "add", "-A")
    sh(path, "git", "commit", "-q", "-m", "init")


def merge_change(repo: Path, name: str, edit) -> None:
    """Make a change on a side branch and merge it with --no-ff, like a mission merge."""
    sh(repo, "git", "checkout", "-q", "-b", name)
    edit(repo)
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-qm", f"[{name}] change")
    sh(repo, "git", "checkout", "-q", "preconquer-gogoymh")
    sh(repo, "git", "merge", "-q", "--no-ff", "-m", f"[{name}] Merge mission", name)


def run(repo: Path, state: Path, *extra: str):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = bench_watch.main(["--repo", str(repo), "--state-dir", str(state), *extra])
    return code, out.getvalue()


class RealHarness(unittest.TestCase):
    """A copy of this repository's committed tree, benchmarked with the real harness (merge suite only)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="goa-bench-test.")
        self.repo = Path(self.tmp.name) / "repo"
        self.state = Path(self.tmp.name) / "state"
        self.repo.mkdir()
        archive = subprocess.Popen(["git", "archive", "HEAD", "goa", "scripts", "benchmarks"], cwd=ROOT,
                                   stdout=subprocess.PIPE)
        subprocess.run(["tar", "-x", "-C", str(self.repo)], stdin=archive.stdout, check=True)
        archive.stdout.close()
        archive.wait()
        # The working copy of the files under test, so uncommitted edits are tested too.
        for rel in ("scripts/benchmark.py", "scripts/bench_watch.py", "benchmarks/tasks.json"):
            (self.repo / rel).write_bytes((ROOT / rel).read_bytes())
        init_repo(self.repo)

    def tearDown(self):
        self.tmp.cleanup()


class NewMergeTest(RealHarness):
    def test_new_merge_is_benchmarked_off_tree_and_recorded(self):
        (self.repo / "scratch.txt").write_text("uncommitted\n")
        status_before = sh(self.repo, "git", "status", "--porcelain")
        worktrees_before = sh(self.repo, "git", "worktree", "list")
        code, out = run(self.repo, self.state, "--suite", "merge", "--iterations", "1")
        self.assertEqual(code, 0, out)
        head = sh(self.repo, "git", "rev-parse", "HEAD")
        self.assertIn("baseline.json", out)  # first run compares with the committed baseline
        rec = json.loads((self.state / "runs" / f"{head[:7]}.json").read_text())
        self.assertTrue(rec["passed"])
        self.assertEqual(rec["commit"], head)
        self.assertEqual(rec["summary"]["merge_accuracy"], 1.0)
        self.assertIn("merge_accuracy", rec["comparison"]["deltas"])
        self.assertEqual(json.loads((self.state / "last.json").read_text())["git"]["commit"], head)
        self.assertFalse((self.state / "regression.md").exists())
        # The run never touched the checkout, the index or the worktree list.
        self.assertEqual(sh(self.repo, "git", "status", "--porcelain"), status_before)
        self.assertEqual(sh(self.repo, "git", "worktree", "list"), worktrees_before)

        # The next merge is compared with that result, not with the baseline.
        merge_change(self.repo, "GOA-99", lambda r: (r / "README.md").write_text("x\n"))
        code, out = run(self.repo, self.state, "--suite", "merge", "--iterations", "1")
        self.assertEqual(code, 0, out)
        new = sh(self.repo, "git", "rev-parse", "HEAD")
        rec = json.loads((self.state / "runs" / f"{new[:7]}.json").read_text())
        self.assertEqual(rec["previous_commit"], head)
        self.assertTrue(any("GOA-99" in m for m in rec["merges"]), rec["merges"])
        self.assertEqual(len((self.state / "history.jsonl").read_text().splitlines()), 2)


class NoNewMergeTest(RealHarness):
    def test_same_commit_exits_at_once_without_benchmarking(self):
        code, out = run(self.repo, self.state, "--suite", "merge", "--iterations", "1")
        self.assertEqual(code, 0, out)
        (self.state / "regression.md").write_text("stale\n")
        started = time.monotonic()
        code, out = run(self.repo, self.state, "--suite", "merge", "--iterations", "1")
        self.assertEqual(code, 0)
        self.assertIn("no new merge", out)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(len((self.state / "history.jsonl").read_text().splitlines()), 1)
        self.assertFalse((self.state / "regression.md").exists(), "a stale report must not file a second issue")


class AccuracyRegressionTest(RealHarness):
    def test_score_drop_writes_report_with_suspects_and_repro(self):
        run(self.repo, self.state, "--suite", "merge", "--iterations", "1")

        def break_merge(r: Path):
            p = r / "goa" / "ontology.py"
            src = p.read_text()
            self.assertIn("def merge3(", src)
            p.write_text(src.replace("def merge3(", "def _real_merge3(", 1) + textwrap.dedent("""

                def merge3(base, ours, theirs, *args, **kwargs):
                    return _real_merge3(base, theirs, ours, *args, **kwargs)
                """))

        merge_change(self.repo, "GOA-98", break_merge)
        code, out = run(self.repo, self.state, "--suite", "merge", "--iterations", "1")
        self.assertEqual(code, 0, out)
        self.assertIn("REGRESSION", out)
        report = (self.state / "regression.md").read_text()
        self.assertIn("merge_accuracy", report)
        self.assertIn("goa/ontology.py", report)
        self.assertIn("python3 scripts/benchmark.py --suite merge --case", report)
        self.assertIn("GOA-98", report)
        before = self.state / "runs" / (sh(self.repo, "git", "rev-parse", "--short=7", "HEAD^1") + ".json")
        code, _ = run(self.repo, self.state, "--suite", "merge", "--iterations", "1", "--force",
                      "--fail-on-regression", "--previous", str(before))
        self.assertEqual(code, 1)


FAKE_HARNESS = """
import argparse, json, sys
from pathlib import Path
sys.path.insert(0, {real!r})
from benchmark import compare_results  # noqa: F401  (bench_watch loads it from here)

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--suite"); p.add_argument("--iterations"); p.add_argument("--output")
    a = p.parse_args()
    ms = float(Path("latency.txt").read_text())
    s = {{"overall_accuracy": 1.0, "total_cases": 1, "passed_cases": 1, "scenario_pass_rate": 1.0,
          "submit_ack_p50_ms": ms, "tick_p50_ms": 10.0, "scenario_total_p50_ms": 100.0, "serve_roundtrip_p50_ms": 100.0}}
    Path(a.output).write_text(json.dumps({{"summary": s, "suites": {{}}, "git": {{}}}}))
"""


class LatencyPairedTest(unittest.TestCase):
    """A fake harness reports the latency written in each commit's latency.txt."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="goa-bench-test.")
        self.repo = Path(self.tmp.name) / "repo"
        self.state = Path(self.tmp.name) / "state"
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / "benchmarks").mkdir()
        (self.repo / "scripts" / "benchmark.py").write_text(FAKE_HARNESS.format(real=str(ROOT / "scripts")))
        (self.repo / "benchmarks" / "tasks.json").write_text("{}")
        (self.repo / "benchmarks" / "baseline.json").write_text(json.dumps({"summary": {"submit_ack_p50_ms": 100.0}}))
        (self.repo / "latency.txt").write_text("100")
        init_repo(self.repo)
        self.assertEqual(run(self.repo, self.state)[0], 0)

    def tearDown(self):
        self.tmp.cleanup()

    def _set_last_latency(self, ms):
        last = json.loads((self.state / "last.json").read_text())
        last["summary"]["submit_ack_p50_ms"] = ms
        (self.state / "last.json").write_text(json.dumps(last))

    def test_load_spike_is_not_filed(self):
        # The stored result was taken on a quiet machine; now everything runs slow.
        self._set_last_latency(20)
        merge_change(self.repo, "GOA-97", lambda r: (r / "note.txt").write_text("x"))
        code, out = run(self.repo, self.state)
        self.assertEqual(code, 0, out)
        self.assertIn("re-measuring", out)
        self.assertFalse((self.state / "regression.md").exists(), out)
        rec = json.loads((self.state / "runs" / f"{sh(self.repo, 'git', 'rev-parse', '--short=7', 'HEAD')}.json").read_text())
        self.assertEqual(rec["paired_latency"]["confirmed"], [])
        self.assertTrue(rec["passed"])

    def test_real_slowdown_is_confirmed_side_by_side(self):
        merge_change(self.repo, "GOA-96", lambda r: (r / "latency.txt").write_text("400"))
        code, out = run(self.repo, self.state)
        self.assertEqual(code, 0, out)
        report = (self.state / "regression.md").read_text()
        self.assertIn("submit_ack_p50_ms: 100.0ms -> 400.0ms", report)
        self.assertIn("confirmed side by side", report)
        self.assertIn("GOA-96", report)

    def test_paired_latency_keeps_best_of_each_side(self):
        samples = {("previous", 0): 300.0, ("current", 0): 120.0, ("previous", 1): 110.0, ("current", 1): 500.0}
        best = bench_watch.paired_latency(lambda side, i: {"tick_p50_ms": samples[(side, i)], "other": 1.0}, 2)
        self.assertEqual(best, {"previous": {"tick_p50_ms": 110.0}, "current": {"tick_p50_ms": 120.0}})


if __name__ == "__main__":
    unittest.main()
