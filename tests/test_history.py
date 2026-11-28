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

        # Truncating with a small limit keeps chronological order; a user turn stays with the first answer
        # to its request, so it shows on the page that holds that answer, not on a later one.
        reopened = Agent(self.repo_path, lang="en")
        recent = reopened.history(limit=2)
        self.assertEqual(
            [(i["type"], i.get("n") or i.get("kind")) for i in recent],
            [("answer", "interim"), ("answer", "final")],
        )
        older = reopened.history(limit=2, before=recent[0]["commit"])
        self.assertEqual(
            [(i["type"], i.get("n") or i.get("kind")) for i in older],
            [("user", 2), ("answer", "interim"), ("user", 3), ("answer", "interim")],
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

    def make_conversation(self, turns: int) -> None:
        a = Agent(self.repo_path, lang="en")
        for i in range(turns):
            a.submit("Plan my Busan business trip next week")
            a.tick()
            a.submit("How far along are you?")
            a.submit("The trip got cancelled, you can stop")

    def test_history_reads_no_branch_state(self):
        self.make_conversation(3)  # nine requests, three cancelled branches
        for i in range(10):
            subprocess.run(["git", "-C", self.repo_path, "branch", f"cancelled/req/{100 + i}", "main"], check=True)
        b = Agent(self.repo_path, lang="en")
        kept = [sha for sha, _ in b.repo.log("answers", n=100)
                if "user.txt" in b.repo.git("ls-tree", "--name-only", sha).split()]
        self.assertEqual(len(kept), 9, "each request's first answer keeps the user's message")
        calls = []
        real = b.repo.read_state
        b.repo.read_state = lambda ref: (calls.append(ref), real(ref))[1]
        items = b.history(limit=100)
        self.assertEqual(calls, [], "history read branch state")
        users = [(i["n"], i["text"]) for i in items if i["type"] == "user"]
        self.assertEqual([n for n, _ in users], list(range(1, 10)))
        self.assertEqual(users[1][1], "How far along are you?")
        self.assertEqual(users[2][1], "The trip got cancelled, you can stop")

        # A repo written before every first answer kept user.txt reads only the branch each answer names.
        legacy = os.path.join(self.dir, "legacy")
        old = Agent(legacy, lang="en")
        rec = old.repo.record_answer
        old.repo.record_answer = lambda kind, text, source, extra, user_text="": rec(kind, text, source, extra)
        old.submit("Plan my Busan business trip next week")
        old.tick()
        old.submit("The trip got cancelled, you can stop")
        for i in range(10):
            subprocess.run(["git", "-C", legacy, "branch", f"cancelled/req/{100 + i}", "main"], check=True)
        c = Agent(legacy, lang="en")
        calls.clear()
        real_c = c.repo.read_state
        c.repo.read_state = lambda ref: (calls.append(ref), real_c(ref))[1]
        self.assertEqual([(i["n"], i["text"]) for i in c.history(limit=50) if i["type"] == "user"],
                         [(1, "Plan my Busan business trip next week"), (2, "The trip got cancelled, you can stop")])
        self.assertEqual(calls, ["cancelled/req/1"])

    def test_serve_pages_older_history(self):
        self.make_conversation(4)  # twelve requests, twelve answers
        cmd = '{"type": "history", "limit": 5}\n'
        events = self.run_serve(self.repo_path, "--llm", "offline", "--decision", "local", "--lang", "en",
                                "--history", "4", stdin=cmd * 3)
        pages = [e for e in events if e["type"] == "history"]
        self.assertEqual(len(pages), 4)
        self.assertNotIn("older", pages[0])
        self.assertTrue(pages[0]["more"])
        self.assertEqual([p.get("older") for p in pages[1:]], [True, True, True])
        self.assertEqual([p["more"] for p in pages], [True, True, False, False])
        self.assertEqual(pages[3]["items"], [])
        commits = [i["commit"] for p in reversed(pages) for i in p["items"] if i["type"] == "answer"]
        self.assertEqual(len(commits), 12)
        self.assertEqual(len(set(commits)), 12, "pages overlap")
        users = [i["n"] for p in reversed(pages) for i in p["items"] if i["type"] == "user"]
        self.assertEqual(users, list(range(1, 13)))
        replay = Agent(self.repo_path, lang="en").history(limit=100)
        self.assertEqual(commits, [i["commit"] for i in replay if i["type"] == "answer"])


if __name__ == "__main__":
    unittest.main()
