"""`goa serve`: JSON lines over stdin/stdout, run as a real child process the way the terminal UI runs it."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from goa import serve

ROOT = Path(__file__).resolve().parent.parent


class Serve(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="goa-serve-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GOA_")}
        self.env.update(GOA_ENV_FILE=os.devnull, GOA_CONFIG=os.path.join(self.tmp, "config"),
                        PYTHONPATH=str(ROOT), GOA_LOG="info")

    def argv(self, *extra):
        return [sys.executable, "-m", "goa", "serve", "--lang", "en", "--repo", os.path.join(self.tmp, "conv"), *extra]

    def piped(self, lines, *extra):
        """Write every line, close stdin (end of input: serve finishes the work), return the events."""
        p = subprocess.run(self.argv(*extra), input="".join(l + "\n" for l in lines), capture_output=True,
                           text=True, env=self.env, cwd=self.tmp, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr)
        events = [json.loads(l) for l in p.stdout.splitlines()]      # stdout is pure JSON, logs went to stderr
        self.assertIn("goa.serve", p.stderr)
        return events

    def test_ready_then_answers_then_final(self):
        events = self.piped([json.dumps({"type": "submit", "text": "Plan my Busan business trip next week"}),
                             json.dumps({"type": "submit", "text": "Oh, keep the budget under 500,000 won"})],
                            "--llm", "offline", "--decision", "local")
        types = [e["type"] for e in events]
        ready = events[0]
        self.assertEqual(ready["type"], "ready")
        self.assertEqual((ready["llm"], ready["decision"], ready["lang"]), ("offline", "local/heuristic", "en"))
        self.assertEqual([e["n"] for e in events if e["type"] == "user"], [1, 2])
        answers = [e for e in events if e["type"] == "answer"]
        self.assertEqual([a["kind"] for a in answers], ["interim", "interim", "final"])
        self.assertEqual((answers[1]["relation"], answers[1]["relation_name"]), ("k1", "supplement"))
        final = answers[-1]
        self.assertTrue(final["text"].startswith("Here is your plan"))
        self.assertIn("within the ₩500,000 budget", final["text"])
        self.assertRegex(final["source"], r"^main@[0-9a-f]{7}$")
        self.assertRegex(final["commit"], r"^[0-9a-f]{7,40}$")
        self.assertIn("step", types)
        self.assertEqual(types.count("submitted"), 2)
        tasks = [e["open"] for e in events if e["type"] == "tasks"]
        self.assertEqual(tasks[0], [{"n": 1, "title": "Busan trip plan", "branch": "req/1"}])
        self.assertEqual(tasks[-1], [])                                  # idle at the end
        self.assertLess(types.index("user"), types.index("answer"))   # the message is echoed before its answer

    def test_plain_lines_bad_json_and_unknown_commands(self):
        events = self.piped(["What time is tomorrow's meeting?", "{not json", json.dumps({"type": "dance"})],
                            "--llm", "offline", "--decision", "local")
        errors = [e["message"] for e in events if e["type"] == "error"]
        self.assertEqual(len(errors), 2)
        self.assertIn("bad JSON", errors[0])
        self.assertIn("unknown command type 'dance'", errors[1])
        final = [e for e in events if e["type"] == "answer"][-1]
        self.assertEqual((final["kind"], final["text"]), ("final", "Here is what I found: Tomorrow's meeting at 2:00 PM."))

    def test_interactive_log_and_quit(self):
        p = subprocess.Popen(self.argv("--llm", "offline", "--decision", "local"), stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=self.env, cwd=self.tmp)
        self.addCleanup(p.stdout.close)
        self.addCleanup(p.stdin.close)
        self.addCleanup(lambda: p.poll() is None and p.kill())

        def until(kind, pred=lambda e: True):
            for line in p.stdout:
                e = json.loads(line)
                if e["type"] == kind and pred(e):
                    return e
            self.fail(f"no {kind} event")

        def send(cmd):
            p.stdin.write(json.dumps(cmd) + "\n")
            p.stdin.flush()

        self.assertEqual(until("ready")["llm"], "offline")
        send({"type": "submit", "text": "Plan my Busan business trip next week"})
        until("answer", lambda e: e["kind"] == "final")
        send({"type": "log"})
        self.assertIn("Merge req/1: Busan trip plan", until("log")["text"])
        send({"type": "quit"})
        self.assertEqual(p.wait(timeout=20), 0)

    def test_a_message_mid_task_joins_the_request_in_progress(self):
        p = subprocess.Popen(self.argv("--llm", "offline", "--decision", "local"), stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=self.env, cwd=self.tmp)
        self.addCleanup(p.stdout.close)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        p.stdin.write(json.dumps({"type": "submit", "text": "Plan my Busan business trip next week"}) + "\n")
        p.stdin.flush()
        events = []
        for line in p.stdout:
            events.append(json.loads(line))
            if events[-1]["type"] == "submitted":
                break                                                    # R1 is open and working: interrupt it now
        p.stdin.write(json.dumps({"type": "submit", "text": "Oh, keep the budget under 500,000 won"}) + "\n")
        p.stdin.close()                                                  # end of input: serve finishes the work
        events += [json.loads(l) for l in p.stdout]
        self.assertEqual(p.wait(timeout=60), 0)
        answers = [e for e in events if e["type"] == "answer"]
        self.assertEqual([(a["kind"], a["relation"]) for a in answers], [("interim", ""), ("interim", "k1"), ("final", "")])
        self.assertIn("The budget (₩500,000) came from your second request.", answers[-1]["text"])

    def test_sign_ins_through_serve_with_mock(self):
        events = self.piped([json.dumps({"type": "submit", "text": "What time is tomorrow's meeting?"})],
                            "--mock", "--llm", "claude-code", "--decision", "llm:codex")
        self.assertEqual((events[0]["llm"], events[0]["model"], events[0]["decision"]),
                         ("claude-code", "sonnet", "llm/codex"))
        final = [e for e in events if e["type"] == "answer"][-1]
        self.assertEqual(final["kind"], "final")
        self.assertIn("2:00 PM", final["text"])

    def test_auto_without_any_connection_is_offline(self):
        self.env.update(GOA_CLAUDE_BIN="goa-missing-claude", GOA_CODEX_BIN="goa-missing-codex")
        for k in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "XAI_API_KEY",
                  "LLAMA_API_KEY"):
            self.env.pop(k, None)
        events = self.piped([])
        self.assertEqual((events[0]["llm"], events[0]["decision"]), ("offline", "local/heuristic"))


class SlowAgent:
    """Busy for 40 steps of 20 ms, each holding the agent lock, like a slow LLM step."""
    llm = type("L", (), {"name": "slow", "model": ""})()
    decision = type("D", (), {"name": "slow"})()
    lang, n, pending = "en", 0, None
    repo = type("R", (), {"path": "/x"})()

    def __init__(self):
        self.lock, self.ticks, self.at = threading.RLock(), 0, None

    def busy(self):
        return self.ticks < 40

    def open_tasks(self):
        return []

    def tick(self):
        with self.lock:
            time.sleep(0.02)
            self.ticks += 1

    def submit(self, text):
        with self.lock:
            self.at = self.ticks


class Fairness(unittest.TestCase):
    def test_a_message_waits_for_one_step_not_the_whole_task(self):
        agent = SlowAgent()
        s = serve.Server(lambda on_answer, on_step: agent, io.StringIO(), io.StringIO())
        worker = threading.Thread(target=s.work, daemon=True)
        worker.start()
        self.addCleanup(s.stop.set)
        time.sleep(0.1)
        sent = agent.ticks
        s.handle(json.dumps({"type": "submit", "text": "and one more thing"}))
        self.assertLessEqual(agent.at - sent, 2, "the worker kept the lock for the whole task")
        self.assertLess(agent.at, 40)


if __name__ == "__main__":
    unittest.main()
