"""Every new request is answered at once, before slow work, and the CLI shows that work is moving."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock as um

from goa import Agent, cli

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class RecordingLLM:
    """An online LLM that logs every call, to prove the 'started' answer comes before (and without) it."""
    name, offline = "recording", False

    def __init__(self, events):
        self.events, self.steps = events, 0

    def complete(self, system, prompt, schema=None, max_tokens=1024):
        self.events.append("llm")
        if schema is None:
            return "LLM answer"
        if "Return only the Constraint" in prompt:
            return json.dumps({"claims": []})
        self.steps += 1
        if self.steps == 1:
            return json.dumps({"done": False, "remaining": [], "claims": [
                {"id": "plan.outline", "type": "Decision", "value": "3 parts", "label": "outline", "depends_on": []}]})
        return json.dumps({"done": True, "remaining": [], "claims": []})

    def json(self, system, prompt, schema, max_tokens=1024):
        return json.loads(self.complete(system, prompt, schema, max_tokens))


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="goa-test-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class FirstRequest(Base):
    def test_first_request_gets_started_answer(self):
        for lang, msg, phrase in (("en", "Plan my Busan business trip next week", "started on “Busan trip plan”"),
                                  ("ko", "다음 주 부산 출장 일정 짜줘", "‘부산 출장 일정’ 작업을 시작했습니다")):
            d = tempfile.mkdtemp(prefix="goa-test-")
            try:
                [ack] = Agent(d, lang=lang).submit(msg)
                self.assertEqual(ack.kind, "interim")
                self.assertIn(phrase, ack.text)
            finally:
                shutil.rmtree(d, ignore_errors=True)

    def test_started_answer_comes_before_any_llm_call(self):
        events = []
        a = Agent(self.dir, llm=RecordingLLM(events), lang="en", on_answer=lambda ans: events.append("answer"))
        [ack] = a.submit("Write a tutorial")
        self.assertEqual(events[0], "answer")            # said before interpret ran
        self.assertIn("Write a tutorial", ack.text)       # template, not "LLM answer": no round trip


class IndependentFollowUp(Base):
    def test_independent_request_is_acknowledged_at_submit(self):
        a = Agent(self.dir, lang="en")
        a.submit("Plan my Busan business trip next week"); a.tick(); a.tick()
        [ack] = a.submit("By the way, what time is tomorrow's meeting?")
        self.assertEqual((ack.kind, ack.relation), ("interim", "k3"))
        self.assertIn("keep working on “Busan trip plan”", ack.text)
        result, final = a.run()                            # the real answer still arrives on merge
        self.assertIn("Tomorrow's meeting is at 2:00 PM", result.text)
        self.assertEqual(final.kind, "final")


class LiveProgress(Base):
    def test_every_work_commit_reports_a_step(self):
        steps = []
        a = Agent(self.dir, lang="en", on_step=lambda b, s: steps.append((b, s)))
        a.submit("Plan my Busan business trip next week")
        a.run()
        commits = a.repo.git("rev-list", "--count", f"{a.tasks[0].base}..req/1").strip()
        self.assertEqual(len(steps), int(commits))
        self.assertTrue(all(b == "req/1" for b, _ in steps))

    def test_terminal_shows_and_clears_status_line(self):
        out = io.StringIO()
        with um.patch.object(cli, "_TTY", True), um.patch.object(sys, "stdout", out):
            live = cli.Live()
            live.show("req/1 · plan.train: decide")
            live.clear()
        self.assertIn("⋯ req/1 · plan.train: decide", out.getvalue())
        self.assertTrue(out.getvalue().endswith("\r\033[K"))


class Demo(unittest.TestCase):
    def test_every_request_line_is_followed_by_an_interim(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GOA_")}
        env.update(GOA_ENV_FILE=os.devnull, NO_COLOR="1")
        out = subprocess.run([sys.executable, "-m", "goa", "demo", "--lang", "en"], cwd=ROOT, env=env,
                             capture_output=True, text=True, timeout=60).stdout
        lines = out.splitlines()
        rows = [i for i, l in enumerate(lines) if l[:1] == "R" and l[1:2].isdigit()]
        self.assertEqual(len(rows), 3, out)
        for i in rows:
            self.assertTrue(lines[i + 1].startswith("  interim "), f"no interim right after {lines[i]!r}")


if __name__ == "__main__":
    unittest.main()
