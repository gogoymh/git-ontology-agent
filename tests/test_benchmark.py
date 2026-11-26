"""Tests for the fixed benchmark task suite, headless runner, baseline record, and regression comparator."""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASKS_PATH = ROOT / "benchmarks" / "tasks.json"
BASELINE_PATH = ROOT / "benchmarks" / "baseline.json"
SCRIPT_PATH = ROOT / "scripts" / "benchmark.py"

if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import benchmark as bench  # noqa: E402


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GOA_")}
    env["GOA_ENV_FILE"] = os.devnull
    env["NO_COLOR"] = "1"
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


class TaskSuiteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(TASKS_PATH.is_file(), f"missing {TASKS_PATH}")
        self.tasks = json.loads(TASKS_PATH.read_text(encoding="utf-8"))

    def test_schema_and_unique_ids(self) -> None:
        self.assertEqual(self.tasks["schema_version"], 1)
        self.assertTrue(self.tasks["name"])
        all_ids = []
        for section in ("relation_cases", "invalidation_cases", "merge_cases", "scenario_cases"):
            items = self.tasks.get(section)
            self.assertIsInstance(items, list)
            self.assertGreater(len(items), 0, f"empty {section}")
            all_ids.extend(c["id"] for c in items)
        self.assertEqual(len(all_ids), len(set(all_ids)), "duplicate benchmark case ids")

    def test_relation_coverage(self) -> None:
        rels = {c["expected_relation"] for c in self.tasks["relation_cases"]}
        self.assertTrue({"k1", "k2", "k3", "k4", "k5", "clarify"}.issubset(rels))
        langs = {c["lang"] for c in self.tasks["relation_cases"]}
        self.assertEqual(langs, {"en", "ko"})
        self.assertTrue(any("expected_target_branch" in c for c in self.tasks["relation_cases"]))

    def test_invalidation_merge_and_scenario_coverage(self) -> None:
        inv_modes = {c["mode"] for c in self.tasks["invalidation_cases"]}
        self.assertEqual(inv_modes, {"state_invalidate", "state_correct", "agent_flow"})

        merge_cases = self.tasks["merge_cases"]
        self.assertTrue(any(c["expected_conflict_count"] > 0 and c["expected_unresolved_count"] == 0 for c in merge_cases))
        self.assertTrue(any(c["expected_unresolved_count"] > 0 for c in merge_cases))

        sc_cases = self.tasks["scenario_cases"]
        self.assertTrue(any(c["mode"] == "agent" and not c.get("mock") for c in sc_cases))
        self.assertTrue(any(c["mode"] == "agent" and c.get("mock") for c in sc_cases))
        self.assertTrue(any(c["mode"] == "serve" for c in sc_cases))


class HeadlessRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="goa-test-bench-run-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_headless_json_run_and_output_file(self) -> None:
        out_file = self.tmp / "result.json"
        res = _run_cli("--iterations", "1", "--json", "--output", str(out_file))
        self.assertEqual(res.returncode, 0, res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(data, json.loads(out_file.read_text(encoding="utf-8")))

        self.assertEqual(data["schema_version"], 1)
        summary = data["summary"]
        for k in (
            "overall_accuracy",
            "relation_accuracy",
            "invalidation_exact_match",
            "invalidation_f1",
            "merge_accuracy",
            "scenario_pass_rate",
        ):
            self.assertIn(k, summary)
            self.assertGreaterEqual(summary[k], 0.0)
            self.assertLessEqual(summary[k], 1.0)

        for k in ("submit_ack_p50_ms", "tick_p50_ms", "scenario_total_p50_ms", "serve_roundtrip_p50_ms"):
            self.assertIn(k, summary)
            self.assertGreater(summary[k], 0.0)

        for suite_name in ("relation", "invalidation", "merge", "scenarios"):
            self.assertIn(suite_name, data["suites"])
            self.assertGreater(data["suites"][suite_name]["total"], 0)

    def test_single_case_reproduction_filter(self) -> None:
        res = _run_cli("--suite", "relation", "--case", "rel_k1_budget_en", "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(list(data["suites"].keys()), ["relation"])
        self.assertEqual(data["suites"]["relation"]["total"], 1)
        self.assertEqual(data["suites"]["relation"]["cases"][0]["id"], "rel_k1_budget_en")

    def test_network_is_blocked_inside_isolated_env(self) -> None:
        with bench._isolated_env():
            with self.assertRaises(RuntimeError):
                urllib.request.urlopen("https://example.com")


class BaselineRecordTest(unittest.TestCase):
    def test_baseline_file_metadata_and_schema(self) -> None:
        self.assertTrue(BASELINE_PATH.is_file(), f"missing {BASELINE_PATH}")
        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        tasks = json.loads(TASKS_PATH.read_text(encoding="utf-8"))

        self.assertEqual(baseline["schema_version"], 1)
        self.assertEqual(baseline["benchmark_name"], tasks["name"])
        git_meta = baseline["git"]
        self.assertEqual(git_meta["base_branch"], "preconquer-gogoymh")
        self.assertTrue(git_meta["base_commit"])

        # Verify recorded base commit exists in repository history
        chk = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--verify", f"{git_meta['base_commit']}^{{commit}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(chk.returncode, 0, f"recorded base_commit not found: {git_meta['base_commit']}")

        self.assertEqual(baseline["suites"]["relation"]["total"], len(tasks["relation_cases"]))
        self.assertEqual(baseline["suites"]["invalidation"]["total"], len(tasks["invalidation_cases"]))
        self.assertEqual(baseline["suites"]["merge"]["total"], len(tasks["merge_cases"]))
        self.assertEqual(baseline["suites"]["scenarios"]["total"], len(tasks["scenario_cases"]))

    def test_compare_against_baseline_passes_without_regression(self) -> None:
        # Use generous latency tolerance because test checks may run concurrently on the host;
        # accuracy tolerance stays strict at 0.0.
        res = _run_cli(
            "--compare",
            str(BASELINE_PATH),
            "--iterations",
            "2",
            "--accuracy-tol",
            "0.0",
            "--latency-tol-pct",
            "300.0",
            "--json",
        )
        self.assertEqual(res.returncode, 0, res.stdout + "\n" + res.stderr)
        payload = json.loads(res.stdout)
        self.assertTrue(payload["comparison"]["passed"], payload["comparison"]["regressions"])
        self.assertEqual(payload["comparison"]["regressions"], [])


class ComparisonTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="goa-test-bench-cmp-"))
        self.base = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name: str, data: dict) -> Path:
        p = self.tmp / name
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return p

    def test_compare_files_passes_when_equal_or_within_jitter(self) -> None:
        curr = copy.deepcopy(self.base)
        curr["summary"]["scenario_total_p50_ms"] += 5.0  # tiny jitter within tolerance
        p_base = self._write("base.json", self.base)
        p_curr = self._write("curr.json", curr)

        res = _run_cli("--compare-files", str(p_base), str(p_curr), "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        cmp = json.loads(res.stdout)
        self.assertTrue(cmp["passed"])
        self.assertEqual(cmp["regressions"], [])

    def test_compare_files_detects_accuracy_regression_with_cause_and_repro(self) -> None:
        curr = copy.deepcopy(self.base)
        curr["summary"]["relation_accuracy"] = round(curr["summary"]["relation_accuracy"] - 0.1, 4)
        curr["summary"]["overall_accuracy"] = round(curr["summary"]["overall_accuracy"] - 0.05, 4)
        curr["suites"]["relation"]["cases"][0]["passed"] = False
        reg_case_id = curr["suites"]["relation"]["cases"][0]["id"]

        p_base = self._write("base.json", self.base)
        p_curr = self._write("curr.json", curr)

        res = _run_cli("--compare-files", str(p_base), str(p_curr), "--json")
        self.assertEqual(res.returncode, 1)
        cmp = json.loads(res.stdout)
        self.assertFalse(cmp["passed"])

        rel_reg = next(r for r in cmp["regressions"] if r["metric"] == "relation_accuracy")
        self.assertIn(reg_case_id, rel_reg["regressed_cases"])
        self.assertIn("goa/decision.py", rel_reg["suspect_modules"])
        self.assertIn(f"--suite relation --case {reg_case_id}", rel_reg["reproduce"])

        # Also verify human-readable text mode prints REGRESSION, suspect modules, and reproduce command
        res_txt = _run_cli("--compare-files", str(p_base), str(p_curr))
        self.assertEqual(res_txt.returncode, 1)
        self.assertIn("REGRESSION", res_txt.stdout)
        self.assertIn("suspect_modules", res_txt.stdout)
        self.assertIn("reproduce", res_txt.stdout)

    def test_compare_files_detects_latency_regression(self) -> None:
        curr = copy.deepcopy(self.base)
        curr["summary"]["scenario_total_p50_ms"] = round(curr["summary"]["scenario_total_p50_ms"] * 3.0 + 120.0, 3)

        p_base = self._write("base.json", self.base)
        p_curr = self._write("curr.json", curr)

        res = _run_cli("--compare-files", str(p_base), str(p_curr), "--json")
        self.assertEqual(res.returncode, 1)
        cmp = json.loads(res.stdout)
        self.assertFalse(cmp["passed"])
        lat_reg = next(r for r in cmp["regressions"] if r["metric"] == "scenario_total_p50_ms")
        self.assertEqual(lat_reg["kind"], "latency")
        self.assertIn("goa/gitstore.py", lat_reg["suspect_modules"])
        self.assertIn("--suite scenarios", lat_reg["reproduce"])

    def test_live_compare_fails_on_synthetic_strict_baseline(self) -> None:
        fake_base = copy.deepcopy(self.base)
        fake_base["summary"]["scenario_total_p50_ms"] = 0.01
        p_fake = self._write("strict_base.json", fake_base)

        res = _run_cli(
            "--suite", "scenarios",
            "--case", "e2e_cancel_en_offline",
            "--iterations", "1",
            "--compare", str(p_fake),
            "--latency-tol-pct", "10.0",
            "--latency-tol-ms", "1.0",
        )
        self.assertEqual(res.returncode, 1)
        self.assertIn("REGRESSION", res.stdout)
        self.assertIn("suspect_modules", res.stdout)
        self.assertIn("reproduce", res.stdout)


if __name__ == "__main__":
    unittest.main()
