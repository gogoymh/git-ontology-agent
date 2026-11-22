"""OpenRouter run fixes: Jev as default judge, 429 waits for the reset, no truncated answers,
`python -m goa.cli`. No network: `goa.http.transport` is swapped, `time.sleep` is mocked."""
import json
import os
import re
import subprocess
import sys
import time
import unittest
from unittest import mock

from goa import http
from goa.decision import JevAPI, make_decision
from goa.llm import make_llm
from goa.respond import Responder
from tests.test_providers import JEV_REPLY, RELATION, ProviderTest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OK = {"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}]}


class Script:
    """Transport that replays a list of replies; an HTTPError in the list is raised."""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, url, headers, body, timeout):
        self.calls.append(json.loads(json.dumps(body)))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class DefaultJudge(ProviderTest):
    def test_openrouter_defaults_to_jev_over_decisions_api(self):
        with mock.patch.dict(os.environ, {"GOA_OPENROUTER_DECISION_MODEL": ""}):
            m = make_decision("openrouter")
        self.assertIsInstance(m, JevAPI)
        self.assertEqual((m.model, m.name), ("typesafe/jev-1.14", "openrouter/typesafe/jev-1.14"))
        rec = self.use(JEV_REPLY)
        self.assertEqual(m.decide("s", RELATION)["relation"]["choice"], "k1")
        self.assertEqual(rec.calls[0][0], "https://openrouter.ai/api/alpha/decisions")

    def test_docs_name_the_default(self):
        with open(os.path.join(ROOT, ".env.example")) as f:
            self.assertRegex(f.read(), r"(?m)^# GOA_OPENROUTER_DECISION_MODEL=typesafe/jev-1\.14\b")
        with open(os.path.join(ROOT, "README.md")) as f:
            readme = f.read()
        row = next(l for l in readme.splitlines() if l.startswith("| `openrouter`,"))
        self.assertIn("default `typesafe/jev-1.14`", row)
        self.assertNotIn("openai/gpt-5.1", readme)


class RateLimit(ProviderTest):
    def run_429(self, err):
        script = Script(err, OK)
        http.transport = script
        with mock.patch("goa.http.time.sleep") as sleep, self.assertLogs("goa.http", "WARNING") as logs:
            out = http.post_json("https://openrouter.ai/api/v1/chat/completions",
                                 {"Authorization": "Bearer or-secret"}, {"x": 1})
        self.assertEqual(out, OK)
        self.assertEqual(len(script.calls), 2)
        self.assertNotIn("or-secret", "\n".join(logs.output))
        self.assertTrue(any("rate limited" in l for l in logs.output))
        return sleep.call_args[0][0]

    def test_retry_after_header(self):
        self.assertAlmostEqual(self.run_429(http.HTTPError(429, "{}", {"Retry-After": "7"})), 7)

    def test_ratelimit_reset_epoch_ms_header(self):
        reset = (time.time() + 12) * 1000
        wait = self.run_429(http.HTTPError(429, "{}", {"X-RateLimit-Reset": str(int(reset))}))
        self.assertTrue(10 <= wait <= 12.5, wait)

    def test_openrouter_error_metadata(self):
        body = json.dumps({"error": {"code": 429, "message": "Rate limit exceeded", "metadata": {
            "headers": {"X-RateLimit-Limit": "20", "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset": str(int((time.time() + 30) * 1000))}}}})
        wait = self.run_429(http.HTTPError(429, body))
        self.assertTrue(28 <= wait <= 30.5, wait)

    def test_wait_is_capped(self):
        self.assertEqual(self.run_429(http.HTTPError(429, "{}", {"Retry-After": "3600"})), http.MAX_WAIT)

    def test_no_hint_falls_back_to_backoff(self):
        self.assertEqual(self.run_429(http.HTTPError(429, "rate limited")), 0.5)

    def test_gives_up_after_retries(self):
        http.transport = Script(*[http.HTTPError(429, "{}", {"Retry-After": "1"})] * 3)
        with mock.patch("goa.http.time.sleep"), self.assertLogs("goa.http", "WARNING"):
            with self.assertRaises(http.HTTPError):
                http.post_json("https://x", {}, {})


def cut(text, reasoning="thinking " * 80):
    return {"choices": [{"message": {"content": text, "reasoning": reasoning}, "finish_reason": "length",
                         "native_finish_reason": "max_tokens"}]}


class Truncation(ProviderTest):
    def test_length_retries_with_more_tokens(self):
        script = Script(cut("This is an"), {"choices": [{"message": {"content": [
            {"type": "text", "text": "This is a full answer."}]}, "finish_reason": "stop"}]})
        http.transport = script
        llm = make_llm("openrouter:anthropic/claude-sonnet-5-5")
        with self.assertLogs("goa.llm", "WARNING"):
            self.assertEqual(llm.complete("sys", "hi", None, 400), "This is a full answer.")
        self.assertEqual([c["max_tokens"] for c in script.calls], [400, 800])

    def test_responder_never_ships_a_cut_answer(self):
        http.transport = Script(cut("This is an"), cut("This is an"), cut("This is an"))
        r = Responder(make_llm("openrouter:anthropic/claude-sonnet-5-5"), "en")
        facts = {"kind": "progress", "decided": [["transport", "KTX"]], "found": [], "working_on": ["hotel"]}
        with self.assertLogs("goa", "WARNING"):
            text = r.write(facts)
        self.assertNotEqual(text, "This is an")
        self.assertEqual(text, r.template(facts))
        self.assertEqual([c["max_tokens"] for c in http.transport.calls], [1024, 2048, 4096])


class Entrypoint(unittest.TestCase):
    def test_python_m_goa_cli_runs(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            env = {"PATH": os.environ.get("PATH", ""), "GOA_ENV_FILE": os.devnull}
            out = subprocess.run([sys.executable, "-m", "goa.cli", "demo", "--repo", os.path.join(d, "r")],
                                 cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertRegex(out.stdout, re.compile(r"R1 .*\n", re.S))


if __name__ == "__main__":
    unittest.main()
