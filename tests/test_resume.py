"""Restart recovery: open `req/*` branches resume from their last commit and merge to a final answer."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from goa import Agent, mock

ROOT = Path(__file__).resolve().parent.parent


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="goa-resume-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.repo_path = os.path.join(self.dir, "conv")

    def git(self, agent: Agent, *args: str) -> str:
        return agent.repo.git(*args)


class AgentResume(Base):
    def test_resumes_open_branch_without_redoing_confirmed_steps(self):
        first = Agent(self.repo_path, lang="en")
        first.submit("Plan my Busan business trip next week")
        first.tick()  # plan.train
        first.tick()  # hotel.candidates
        before_log = self.git(first, "log", "--format=%s", "req/1").splitlines()
        self.assertEqual(len(before_log), 4)  # C0 + open + 2 steps
        self.assertEqual(self.git(first, "log", "--merges", "--format=%s", "main"), "")

        # Restart: a brand new Agent on the same git repo restores req/1 and finishes it.
        second = Agent(self.repo_path, lang="en")
        self.assertEqual([(t.n, t.branch, t.title, t.requests) for t in second.open_tasks()],
                         [(1, "req/1", "Busan trip plan", ["req.1"])])
        self.assertEqual(second.n, 1)
        self.assertGreaterEqual(second.clock, 3)

        answers = second.run()
        self.assertEqual([a.kind for a in answers], ["final"])
        self.assertIn("Here is your plan: Tue 08:00 KTX (Seoul→Busan)", answers[0].text)
        self.assertIn("Hotel C (2 nights, ₩420,000)", answers[0].text)
        self.assertEqual(self.git(second, "log", "--merges", "--format=%s", "main"),
                         "Merge req/1: Busan trip plan")
        after_log = self.git(second, "log", "--format=%s", "req/1").splitlines()
        # Only plan.hotel and plan.total were added; confirmed steps were not redone.
        self.assertEqual(len(after_log), 6)
        self.assertEqual(after_log[-4:], before_log)

    def test_resumes_after_k1_supplement_with_n_clock_and_provenance(self):
        first = Agent(self.repo_path, lang="en")
        first.submit("Plan my Busan business trip next week")
        first.tick()
        first.tick()
        [sup] = first.submit("Oh, keep the budget under 500,000 won")
        self.assertEqual(sup.relation, "k1")
        clock_before = first.clock

        second = Agent(self.repo_path, lang="en")
        [task] = second.open_tasks()
        self.assertEqual(task.requests, ["req.1", "req.2"])
        self.assertEqual(second.n, 2)
        self.assertEqual(second.clock, clock_before)

        # Follow-up after restart gets req.3 (does not collide with req.2) and sees the open task.
        [prog] = second.submit("How far along are you?")
        self.assertEqual((prog.relation, prog.request), ("k5", "req.3"))
        self.assertIn("KTX is set", prog.text)

        [final] = second.run()
        self.assertEqual(final.kind, "final")
        self.assertIn("Hotel A (2 nights, ₩180,000)", final.text)
        self.assertIn("total ₩460,000 (within the ₩500,000 budget)", final.text)
        self.assertIn("The budget (₩500,000) came from your second request.", final.text)

    def test_resumes_after_k2_correction_preserves_title_and_provenance(self):
        first = Agent(self.repo_path, lang="en")
        first.submit("Plan my Busan business trip next week")
        first.tick()
        first.tick()
        [corr] = first.submit("Actually, not Busan but Daegu")
        self.assertEqual(corr.relation, "k2")

        second = Agent(self.repo_path, lang="en")
        [task] = second.open_tasks()
        self.assertEqual(task.title, "Daegu trip plan")
        self.assertEqual(task.requests, ["req.1", "req.2"])

        [final] = second.run()
        self.assertEqual(final.kind, "final")
        self.assertIn("Tue 08:30 KTX (Seoul→Dongdaegu)", final.text)
        self.assertIn("The trip city was changed to Daegu in your second request.", final.text)
        self.assertEqual(self.git(second, "log", "--merges", "--format=%s", "main"),
                         "Merge req/1: Daegu trip plan")

    def test_resumes_when_all_steps_done_and_only_merge_remains(self):
        first = Agent(self.repo_path, lang="en")
        first.submit("Plan my Busan business trip next week")
        for _ in range(4):
            first.tick()
        self.assertEqual(self.git(first, "log", "--merges", "--format=%s", "main"), "")

        second = Agent(self.repo_path, lang="en")
        self.assertEqual(len(second.open_tasks()), 1)
        [final] = second.tick()
        self.assertEqual(final.kind, "final")
        self.assertFalse(second.busy())
        self.assertEqual(self.git(second, "log", "--merges", "--format=%s", "main"),
                         "Merge req/1: Busan trip plan")


class BranchStates(Base):
    def test_merged_and_cancelled_branches_are_not_reopened(self):
        first = Agent(self.repo_path, lang="en")
        first.submit("Plan my Busan business trip next week")
        first.run()

        after_merge = Agent(self.repo_path, lang="en")
        self.assertEqual(after_merge.open_tasks(), [])
        self.assertFalse(after_merge.busy())
        self.assertEqual(after_merge.run(), [])
        self.assertEqual(after_merge.n, 1)

        cancel_repo = os.path.join(self.dir, "cancel")
        cancelling = Agent(cancel_repo, lang="en")
        cancelling.submit("Plan my Busan business trip next week")
        cancelling.tick()
        [cancel] = cancelling.submit("The trip got cancelled, you can stop")
        self.assertEqual(cancel.relation, "k4")

        after_cancel = Agent(cancel_repo, lang="en")
        self.assertEqual(after_cancel.open_tasks(), [])
        self.assertFalse(after_cancel.busy())
        self.assertEqual(after_cancel.run(), [])
        self.assertEqual(after_cancel.n, 1)

    def test_resumes_parallel_root_and_k3_independent_branches(self):
        first = Agent(self.repo_path, lang="en")
        first.submit("Plan my Busan business trip next week")
        first.tick()
        [ack] = first.submit("By the way, what time is tomorrow's meeting?")
        self.assertEqual(ack.relation, "k3")

        second = Agent(self.repo_path, lang="en")
        self.assertEqual([(t.n, t.independent) for t in second.open_tasks()], [(1, False), (2, True)])

        out = second.run()
        self.assertEqual([(a.kind, a.relation) for a in out], [("interim", "k3"), ("final", "")])
        self.assertIn("Tomorrow's meeting is at 2:00 PM", out[0].text)
        self.assertIn("Still working on the Busan trip plan", out[0].text)
        self.assertIn("Here is your plan: Tue 08:00 KTX (Seoul→Busan)", out[1].text)
        self.assertNotIn("Tomorrow", out[1].text)

    def test_resumes_when_root_merged_before_restart_and_independent_remains_open(self):
        first = Agent(self.repo_path, lang="en")
        first.submit("What time is tomorrow's meeting?")
        [ack] = first.submit("Can you plan my Busan business trip next week?")
        self.assertEqual(ack.relation, "k3")
        # 2 ticks finish and merge req/1 (calendar), while req/2 (trip) is still open.
        first.tick()
        first.tick()
        self.assertEqual([t.branch for t in first.open_tasks()], ["req/2"])
        self.assertEqual(self.git(first, "log", "--merges", "--format=%s", "main"),
                         "Merge req/1: tomorrow's meeting")

        second = Agent(self.repo_path, lang="en")
        self.assertEqual([t.branch for t in second.open_tasks()], ["req/2"])
        out = second.run()
        self.assertEqual([(a.kind, a.relation) for a in out], [("interim", "k3"), ("final", "")])
        self.assertIn("Here is what I found: Tomorrow's meeting at 2:00 PM.", out[1].text)


class ServeResume(Base):
    def setUp(self):
        super().setUp()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GOA_")}
        self.env.update(GOA_ENV_FILE=os.devnull, GOA_CONFIG=os.path.join(self.dir, "config"),
                        PYTHONPATH=str(ROOT), GOA_LOG="info")

    def run_serve(self, *extra: str, stdin: str = "") -> list:
        argv = [sys.executable, "-m", "goa", "serve", "--lang", "en", "--repo", self.repo_path, *extra]
        p = subprocess.run(argv, input=stdin, capture_output=True, text=True, env=self.env, cwd=self.dir, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr)
        return [json.loads(line) for line in p.stdout.splitlines() if line.strip()]

    def test_serve_restart_emits_open_tasks_and_finishes_final_answer(self):
        # Simulate a previous session stopping with req/1 open after 2 commits.
        first = Agent(self.repo_path, lang="en")
        first.submit("Plan my Busan business trip next week")
        first.tick()
        first.tick()

        # Starting `goa serve` with no new input resumes req/1, emits tasks, and produces the final answer.
        events = self.run_serve("--llm", "offline", "--decision", "local")
        self.assertEqual(events[0]["type"], "ready")
        self.assertEqual(events[1], {"type": "tasks", "open": [{"n": 1, "title": "Busan trip plan", "branch": "req/1"}]})
        answers = [e for e in events if e["type"] == "answer"]
        self.assertEqual([a["kind"] for a in answers], ["final"])
        self.assertIn("Here is your plan: Tue 08:00 KTX (Seoul→Busan)", answers[0]["text"])
        self.assertRegex(answers[0]["source"], r"^main@[0-9a-f]{7}$")
        tasks_events = [e["open"] for e in events if e["type"] == "tasks"]
        self.assertEqual(tasks_events[-1], [])

    def test_serve_restart_with_different_model_resumes_and_merges(self):
        # Start with offline, then switch to mocked claude-code mid-request.
        first = Agent(self.repo_path, lang="en")
        first.submit("Plan my Busan business trip next week")
        first.tick()
        first.tick()

        events = self.run_serve("--mock", "--llm", "claude-code", "--decision", "llm:claude-code")
        self.assertEqual(events[0]["llm"], "claude-code")
        self.assertEqual(events[1]["type"], "tasks")
        self.assertEqual(events[1]["open"][0]["branch"], "req/1")
        final = [e for e in events if e["type"] == "answer"][-1]
        self.assertEqual(final["kind"], "final")
        self.assertIn("Tue 08:00 KTX (Seoul→Busan)", final["text"])

        # Reverse direction: open with mocked claude-code, finish with offline.
        repo2 = os.path.join(self.dir, "conv2")
        with mock.session():
            a = Agent(repo2, llm="claude-code", decision="llm:claude-code", lang="en")
            a.submit("Plan my Busan business trip next week")
            a.tick()
            a.tick()
        p = subprocess.run(
            [sys.executable, "-m", "goa", "serve", "--lang", "en", "--repo", repo2,
             "--llm", "offline", "--decision", "local"],
            input="", capture_output=True, text=True, env=self.env, cwd=self.dir, timeout=120,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        ev2 = [json.loads(line) for line in p.stdout.splitlines() if line.strip()]
        final2 = [e for e in ev2 if e["type"] == "answer"][-1]
        self.assertEqual(final2["kind"], "final")
        self.assertIn("Here is your plan: Tue 08:00 KTX (Seoul→Busan)", final2["text"])


if __name__ == "__main__":
    unittest.main()
