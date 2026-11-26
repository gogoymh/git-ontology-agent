"""Conversation history replay from git (`main`, `answers`, open and cancelled `req/*` branches)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from goa import Agent

ROOT = Path(__file__).resolve().parent.parent


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="goa-history-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.repo_path = os.path.join(self.dir, "conv")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GOA_")}
        self.env.update(GOA_ENV_FILE=os.devnull, GOA_CONFIG=os.path.join(self.dir, "config"),
                        PYTHONPATH=str(ROOT), GOA_LOG="info")

    def run_serve(self, repo: str, *extra: str, stdin: str = "") -> list:
        argv = [sys.executable, "-m", "goa", "serve", "--repo", repo, *extra]
        p = subprocess.run(argv, input=stdin, capture_output=True, text=True, env=self.env, cwd=self.dir, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr)
        return [json.loads(line) for line in p.stdout.splitlines() if line.strip()]

    def test_serve_replays_completed_turns_in_order(self):
        # Empty repo emits no history event.
        empty_events = self.run_serve(os.path.join(self.dir, "empty"), "--llm", "offline", "--decision", "local", "--lang", "en")
        self.assertEqual([e["type"] for e in empty_events], ["ready"])

        # Complete a multi-turn conversation (root + k1 supplement + k3 independent).
        first = Agent(self.repo_path, lang="en")
        first.submit("Plan my Busan business trip next week")
        first.tick()
        first.tick()
        first.submit("Oh, keep the budget under 500,000 won")
        first.submit("By the way, what time is tomorrow's meeting?")
        first.run()

        events = self.run_serve(self.repo_path, "--llm", "offline", "--decision", "local", "--lang", "en")
        self.assertEqual(events[0]["type"], "ready")
        hist = [e for e in events if e["type"] == "history"]
        self.assertEqual(len(hist), 1)
        items = hist[0]["items"]

        # Exact chronological sequence of user turns and answers without duplicating user turns.
        summary = [
            ("user", i["n"], i["text"]) if i["type"] == "user"
            else ("answer", i["kind"], i["request"], i["relation"], i["relation_name"])
            for i in items
        ]
        self.assertEqual(summary, [
            ("user", 1, "Plan my Busan business trip next week"),
            ("answer", "interim", "req.1", "", ""),
            ("user", 2, "Oh, keep the budget under 500,000 won"),
            ("answer", "interim", "req.2", "k1", "supplement"),
            ("user", 3, "By the way, what time is tomorrow's meeting?"),
            ("answer", "interim", "req.3", "k3", "independent"),
            ("answer", "interim", "req.3", "k3", "independent"),
            ("answer", "final", "req.1, req.2", "", ""),
        ])

        answers = [i for i in items if i["type"] == "answer"]
        self.assertIn("Tomorrow's meeting is at 2:00 PM", answers[3]["text"])
        self.assertIn("within the ₩500,000 budget", answers[4]["text"])
        self.assertRegex(answers[4]["source"], r"^main@[0-9a-f]{7}$")
        self.assertRegex(answers[4]["commit"], r"^[0-9a-f]{7,40}$")

        # Truncating with a small limit keeps chronological order and never emits older requests out of order.
        reopened = Agent(self.repo_path, lang="en")
        recent = reopened.history(limit=2)
        self.assertEqual(
            [(i["type"], i.get("n") or i.get("kind")) for i in recent],
            [("user", 3), ("answer", "interim"), ("answer", "final")],
        )

        # Multi-byte UTF-8 (Korean) conversation replays intact.
        ko_repo = os.path.join(self.dir, "ko")
        ko = Agent(ko_repo, lang="ko")
        ko.submit("다음 주 부산 출장 일정 짜줘")
        ko.run()
        ko_events = self.run_serve(ko_repo, "--llm", "offline", "--decision", "local", "--lang", "ko")
        ko_items = [e for e in ko_events if e["type"] == "history"][0]["items"]
        self.assertEqual(ko_items[0], {"type": "user", "n": 1, "text": "다음 주 부산 출장 일정 짜줘"})
        self.assertEqual(ko_items[-1]["kind"], "final")
        self.assertIn("부산", ko_items[-1]["text"])

    def test_serve_replays_open_branch_and_resumes_in_order(self):
        first = Agent(self.repo_path, lang="en")
        # First turn: progress query (k5) then cancel (k4).
        first.submit("Plan my Busan business trip next week")
        first.tick()
        first.tick()
        first.submit("How far along are you?")
        first.submit("The trip got cancelled, you can stop")
        # Second turn: open branch with k2 correction, interrupted before completion.
        first.submit("Plan my Busan business trip next week")
        first.tick()
        first.tick()
        first.submit("Actually, not Busan but Daegu")

        events = self.run_serve(self.repo_path, "--llm", "offline", "--decision", "local", "--lang", "en")
        types = [e["type"] for e in events]
        self.assertEqual(events[0]["type"], "ready")
        self.assertEqual(events[1], {"type": "tasks", "open": [{"n": 4, "title": "Daegu trip plan", "branch": "req/4"}]})
        self.assertEqual(events[2]["type"], "history")

        items = events[2]["items"]
        summary = [
            ("user", i["n"], i["text"]) if i["type"] == "user"
            else ("answer", i["kind"], i["request"], i["relation"])
            for i in items
        ]
        self.assertEqual(summary, [
            ("user", 1, "Plan my Busan business trip next week"),
            ("answer", "interim", "req.1", ""),
            ("user", 2, "How far along are you?"),
            ("answer", "interim", "req.2", "k5"),
            ("user", 3, "The trip got cancelled, you can stop"),
            ("answer", "interim", "req.3", "k4"),
            ("user", 4, "Plan my Busan business trip next week"),
            ("answer", "interim", "req.4", ""),
            ("user", 5, "Actually, not Busan but Daegu"),
            ("answer", "interim", "req.5", "k2"),
        ])

        # Resumed live final answer arrives strictly after the replayed history event.
        live_answers = [(idx, e) for idx, e in enumerate(events) if e["type"] == "answer"]
        self.assertEqual(len(live_answers), 1)
        final_idx, final = live_answers[0]
        self.assertGreater(final_idx, types.index("history"))
        self.assertEqual(final["kind"], "final")
        self.assertIn("Tue 08:30 KTX (Seoul→Dongdaegu)", final["text"])
        self.assertIn("changed to Daegu", final["text"])
        self.assertEqual([e["open"] for e in events if e["type"] == "tasks"][-1], [])


if __name__ == "__main__":
    unittest.main()
