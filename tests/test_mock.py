"""`--mock`: the online path (real request builders, reply parsers, LLMSkill, Responder) against
simulated provider APIs, with no key and no network."""
import contextlib
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock as um

from goa import Agent, http, mock
from goa.cli import SCENARIOS, main
from goa.llm import DEFAULT_MODELS

LLMS = sorted(DEFAULT_MODELS)
DECISION_SPECS = ["typesafe", "openai", "gemini", "cloudflare", "openrouter", "aimlapi", "llm:anthropic", "local"]
EXPECT = {  # scenario -> phrases the last answer must carry (same as the offline spec output)
    "budget": ["Hotel A (2 nights, ₩180,000)", "total ₩460,000 (within the ₩500,000 budget)",
               "came from your second request"],
    "correction": ["Tue 08:30 KTX (Seoul→Dongdaegu)", "Hotel B (2 nights, ₩160,000)",
                   "changed to Daegu in your second request"],
    "cancel": ["Stopped the Busan trip plan", "stays in history only"],
}


def _no_network(*a, **k):
    raise AssertionError("network used in mock mode")


class MockBase(unittest.TestCase):
    def setUp(self):
        self.dirs = []
        env = {k: v for k, v in os.environ.items() if k not in mock.FAKE_KEYS}
        self.env = um.patch.dict(os.environ, env, clear=True)
        self.env.start()
        self.net = um.patch("urllib.request.urlopen", _no_network)
        self.net.start()

    def tearDown(self):
        self.net.stop()
        self.env.stop()
        for d in self.dirs:
            shutil.rmtree(d, ignore_errors=True)

    def play(self, scenario, llm, decision):
        d = tempfile.mkdtemp(prefix="goa-mock-")
        self.dirs.append(d)
        with mock.session() as sim:
            agent = Agent(d, llm=llm, decision=decision, lang="en")
            answers = []
            for msg, ticks in SCENARIOS[scenario]["en"]:
                answers += agent.submit(msg)
                answers += agent.run() if ticks is None else [a for _ in range(ticks) for a in agent.tick()]
        return agent, answers, sim


class EveryProvider(MockBase):
    def test_every_llm_and_decision_model(self):
        # every LLM and every decision model at least once, every scenario with each; pairs rotate to stay fast
        scenarios = list(EXPECT)
        pairs = [(LLMS[i % len(LLMS)], d, scenarios[i % len(scenarios)]) for i, d in enumerate(DECISION_SPECS)]
        pairs += [("anthropic", "typesafe", s) for s in scenarios]
        self.assertEqual({p[0] for p in pairs}, set(LLMS))
        for llm, decision, scenario in pairs:
            with self.subTest(scenario=scenario, llm=llm, decision=decision):
                agent, answers, sim = self.play(scenario, llm, decision)
                self.assertFalse(agent.llm.offline)
                self.assertEqual(type(agent.skills[0]).__name__, "LLMSkill")
                for p in EXPECT[scenario]:
                    self.assertIn(p, answers[-1].text)
                used = {c[0] for c in sim.calls}
                self.assertIn(llm, used)
                if decision != "local":
                    self.assertIn(decision.split(":")[-1], used)

    def test_relations_and_sources_match_offline(self):
        _, answers, _ = self.play("budget", "anthropic", "typesafe")
        self.assertEqual([(a.kind, a.relation) for a in answers],
                         [("interim", "k1"), ("interim", "k3"), ("final", "")])
        self.assertIn("redoing the hotel candidates, hotel and total", answers[0].text)
        self.assertIn("Still working on the Busan trip plan", answers[1].text)
        self.assertTrue(answers[0].source.startswith("req/1@"))
        self.assertTrue(answers[2].source.startswith("main@"))

    def test_session_restores_transport_and_keys(self):
        orig = http.transport
        with mock.session():
            self.assertIsNot(http.transport, orig)
            self.assertTrue(os.environ.get("ANTHROPIC_API_KEY"))
        self.assertIs(http.transport, orig)
        self.assertNotIn("ANTHROPIC_API_KEY", os.environ)

    def test_unknown_endpoint_is_an_error(self):
        with mock.session():
            with self.assertRaises(http.HTTPError), self.assertLogs("goa.http", "WARNING"):
                http.post_json("https://example.com/v1/x", {"Authorization": "Bearer k"}, {}, retries=0)


class DemoCommand(MockBase):
    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["demo", "--mock", "--lang", "en", *argv]), 0)
        return out.getvalue()

    def test_demo_mock_prints_simulated_calls(self):
        text = self.run_cli()
        self.assertIn("anthropic/claude-sonnet-5-5 (simulated)", text)
        self.assertIn("typesafe/jev-1.14 (simulated)", text)
        self.assertIn("  api     anthropic ×", text)
        self.assertRegex(text, r"\d+ simulated API calls \(anthropic ×\d+ · typesafe ×\d+\), 0 network requests")
        for p in EXPECT["budget"]:
            self.assertIn(p, text)

    def test_demo_mock_with_other_providers(self):
        text = self.run_cli("--llm", "gemini", "--decision", "openai", "--scenario", "correction")
        self.assertIn("gemini ×", text)
        self.assertIn("openai ×", text)
        for p in EXPECT["correction"]:
            self.assertIn(p, text)


if __name__ == "__main__":
    unittest.main()
