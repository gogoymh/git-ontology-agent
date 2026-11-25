"""`goa ask`: one shot from the shell, plain text or JSON lines; and what bare `goa` does."""
import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock as um

from goa import cli

ROOT = Path(__file__).resolve().parent.parent


class Ask(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="goa-ask-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GOA_")}
        self.env.update(GOA_ENV_FILE=os.devnull, GOA_CONFIG=os.path.join(self.tmp, "config"), PYTHONPATH=str(ROOT),
                        NO_COLOR="1")

    def ask(self, *args, stdin=None, code=0):
        p = subprocess.run([sys.executable, "-m", "goa", "ask", "--lang", "en", "--repo", os.path.join(self.tmp, "conv"),
                            *args], input=stdin, capture_output=True, text=True, env=self.env, cwd=self.tmp, timeout=120)
        self.assertEqual(p.returncode, code, p.stderr)
        return p.stdout, p.stderr

    def test_prints_the_answers_and_exits_zero(self):
        out, _ = self.ask("--llm", "offline", "--decision", "local", "Plan my Busan business trip next week")
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("  Agent (interim)  Got it, I've started on “Busan trip plan”"))
        final = next(l for l in lines if "Agent (final)" in l)
        self.assertIn("Here is your plan: Tue 08:00 KTX (Seoul→Busan)", final)
        self.assertTrue(any(l.strip().startswith("↳ from main@") for l in lines))
        self.assertNotIn("User (R1)", out)                               # one message: just the answers

    def test_several_messages_read_like_a_conversation(self):
        out, _ = self.ask("--llm", "offline", "--decision", "local", "Plan my Busan business trip next week",
                          "Oh, keep the budget under 500,000 won")
        self.assertIn("User (R1)          Plan my Busan business trip next week", out)
        self.assertIn("User (R2)          Oh, keep the budget under 500,000 won", out)
        self.assertIn("k1 supplement", out)
        self.assertIn("The budget (₩500,000) came from your second request.", out)

    def test_json_lines(self):
        out, _ = self.ask("--json", "--llm", "offline", "--decision", "local", "What time is tomorrow's meeting?")
        events = [json.loads(l) for l in out.splitlines()]
        self.assertEqual(events[0]["type"], "ready")
        self.assertEqual(events[1], {"type": "user", "n": 1, "text": "What time is tomorrow's meeting?"})
        answers = [e for e in events if e["type"] == "answer"]
        self.assertEqual([a["kind"] for a in answers], ["interim", "final"])
        self.assertEqual(answers[-1]["text"], "Here is what I found: Tomorrow's meeting at 2:00 PM.")

    def test_messages_from_stdin_and_conversation_persists(self):
        self.ask("--llm", "offline", "--decision", "local", "Plan my Busan business trip next week")
        out, _ = self.ask("--json", "--llm", "offline", "--decision", "local", stdin="What time is tomorrow's meeting?\n")
        user = next(json.loads(l) for l in out.splitlines() if json.loads(l)["type"] == "user")
        self.assertEqual(user["n"], 2)                                   # the same .goa repo: request 2
        log = subprocess.run([sys.executable, "-m", "goa", "log", "--repo", os.path.join(self.tmp, "conv")],
                             capture_output=True, text=True, env=self.env, check=True).stdout
        self.assertIn("Merge req/1", log)
        self.assertIn("Merge req/2", log)

    def test_no_message_is_a_usage_error(self):
        _, err = self.ask("--llm", "offline", stdin="", code=2)
        self.assertIn("goa ask: give a message", err)

    def test_provider_failure_exits_one(self):
        self.env.update(GOA_CLAUDE_BIN="goa-missing-claude")
        out, err = self.ask("--json", "--llm", "claude-code", "--decision", "local", "Name three cats", code=1)
        error = [json.loads(l) for l in out.splitlines() if json.loads(l)["type"] == "error"]
        self.assertIn("goa-missing-claude is not installed", error[0]["message"])


class Bare(unittest.TestCase):
    def test_bare_goa_without_a_terminal_prints_help(self):
        with contextlib.redirect_stdout(io.StringIO()) as out, um.patch.object(cli.env, "load", lambda p: []):
            self.assertEqual(cli.main([]), 2)
        self.assertIn("Run `goa` alone for the terminal UI", out.getvalue())
        self.assertIn("ask", out.getvalue())

    def test_bare_goa_falls_back_to_chat_when_the_ui_cannot_start(self):
        args = argparse.Namespace(cmd=None, llm="auto", decision="auto", lang="", repo=".goa", mock=False)
        with um.patch.object(cli.shutil, "which", lambda b: None), \
                um.patch.object(cli, "cmd_chat", lambda a: 7), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.cmd_ui(args, bare=True), 7)
            self.assertIn("Node.js 22+ is not installed", out.getvalue())
        with um.patch.object(cli.shutil, "which", lambda b: None), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.cmd_ui(args), 1)                        # `goa ui` says why instead
        self.assertIn("goa ui: Node.js 22+ is not installed", err.getvalue())

    def test_ui_gets_this_python_and_the_flags(self):
        seen = {}

        class Proc:
            def __init__(self, argv, env):
                seen.update(argv=argv, env=env)

            def wait(self):
                return 0
        args = argparse.Namespace(cmd="ui", llm="codex", decision="auto", lang="ko", repo=".goa", mock=True)
        with um.patch.object(cli.shutil, "which", lambda b: "/usr/bin/node"), \
                um.patch.object(cli, "_node_major", lambda n: 26), \
                um.patch.object(cli, "_ui_entry", lambda: "/x/ui/dist/cli.js"), \
                um.patch.object(cli.subprocess, "Popen", Proc):
            self.assertEqual(cli.cmd_ui(args), 0)
        self.assertEqual(seen["argv"], ["/usr/bin/node", "/x/ui/dist/cli.js", "--llm", "codex", "--lang", "ko",
                                        "--repo", ".goa", "--mock"])
        self.assertEqual(seen["env"]["GOA_PYTHON"], sys.executable)
        self.assertIn(str(ROOT), seen["env"]["PYTHONPATH"])


if __name__ == "__main__":
    unittest.main()
