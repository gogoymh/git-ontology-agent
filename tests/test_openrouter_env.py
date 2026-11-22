"""OpenRouter as LLM and as decision model, and the `.env` loader. No network."""
import json
import os
import re
import tempfile
import unittest
from unittest import mock

from goa import env
from goa.decision import JevAPI, OpenRouterDecision as ORDecision, make_decision
from goa.llm import make_llm
from tests.test_providers import JEV_REPLY, RELATION, ProviderTest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHAT = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterLLM(ProviderTest):
    def test_chat_completions(self):
        rec = self.use({"choices": [{"message": {"content": "hello"}}]})
        llm = make_llm("openrouter:anthropic/claude-sonnet-5-5")
        self.assertEqual(llm.complete("sys", "hi"), "hello")
        u, h, b = rec.calls[0]
        self.assertEqual((u, h["Authorization"], b["model"]), (CHAT, "Bearer or-k", "anthropic/claude-sonnet-5-5"))

    def test_default_and_env_model(self):
        self.assertEqual(make_llm("openrouter").model, "openrouter/auto")
        with mock.patch.dict(os.environ, {"GOA_OPENROUTER_MODEL": "x/y"}):
            self.assertEqual(make_llm("openrouter").model, "x/y")


class OpenRouterDecision(ProviderTest):
    def test_chat_model_judges_with_strict_schema(self):
        content = json.dumps({"relation": {"choice": "k1", "confidence": 0.94}, "intent": {"noul": 0.81}})
        rec = self.use({"choices": [{"message": {"content": content}}]})
        m = make_decision("openrouter:openai/gpt-5.1")
        self.assertIsInstance(m, ORDecision)
        self.assertEqual(m.name, "openrouter/openai/gpt-5.1")
        a = m.decide({"new": "budget"}, RELATION)
        self.assertEqual((a["relation"]["choice"], a["intent"]["noul"]), ("k1", 0.81))
        u, h, b = rec.calls[0]
        self.assertEqual((u, h["Authorization"], b["model"], b["temperature"]), (CHAT, "Bearer or-k", "openai/gpt-5.1", 0))
        fmt = b["response_format"]
        self.assertEqual((fmt["type"], fmt["json_schema"]["strict"]), ("json_schema", True))
        schema = fmt["json_schema"]["schema"]
        self.assertFalse(schema["additionalProperties"])
        self.assertFalse(schema["properties"]["relation"]["additionalProperties"])
        self.assertEqual(schema["properties"]["relation"]["properties"]["choice"]["enum"], ["k1", "k2", "k3", "k4", "k5"])
        self.assertTrue(b["provider"]["require_parameters"])

    def test_default_model_and_env(self):
        m = make_decision("openrouter")
        self.assertEqual((type(m).__name__, m.model), ("OpenRouterDecision", "openrouter/auto"))
        with mock.patch.dict(os.environ, {"GOA_OPENROUTER_DECISION_MODEL": "google/gemini-2.5-flash"}):
            self.assertEqual(make_decision("openrouter").model, "google/gemini-2.5-flash")

    def test_jev_models_use_decisions_gateway(self):
        for spec in ("openrouter:cloudflare/clef", "openrouter:typesafe/jev-1.14"):
            rec = self.use(JEV_REPLY)
            m = make_decision(spec)
            self.assertIsInstance(m, JevAPI)
            self.assertEqual(m.decide("s", RELATION)["relation"]["choice"], "k1")
            self.assertEqual(rec.calls[0][0], "https://openrouter.ai/api/alpha/decisions")

    def test_missing_key(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
            with self.assertRaisesRegex(RuntimeError, "OPENROUTER_API_KEY"):
                make_decision("openrouter:openai/gpt-5.1").decide("s", RELATION)


class DotEnv(unittest.TestCase):
    def test_loads_without_overriding(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, ".env")
            with open(p, "w") as f:
                f.write("# comment\nGOA_T_A=one\nexport GOA_T_B='two words'\nGOA_T_C=keep\n"
                        "GOA_T_D=x # note\nGOA_T_E=\nnot a line\n")
            with mock.patch.dict(os.environ, {"GOA_T_C": "shell"}):
                self.assertEqual(sorted(env.load(p)), ["GOA_T_A", "GOA_T_B", "GOA_T_D"])
                self.assertEqual([os.environ.get(k) for k in ("GOA_T_A", "GOA_T_B", "GOA_T_C", "GOA_T_D", "GOA_T_E")],
                                 ["one", "two words", "shell", "x", None])
            self.assertEqual(env.load(os.path.join(d, "missing")), [])

    def test_cli_reads_env_file(self):
        from goa import cli
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, ".env")
            with open(p, "w") as f:
                f.write("GOA_LLM=openrouter\n")
            with mock.patch.dict(os.environ, {"GOA_ENV_FILE": p}):
                os.environ.pop("GOA_LLM", None)
                with mock.patch.object(cli, "cmd_log", lambda a: 0):
                    cli.main(["log"])
                self.assertEqual(os.environ.get("GOA_LLM"), "openrouter")

    def test_example_lists_every_key(self):
        with open(os.path.join(ROOT, ".env.example")) as f:
            text = f.read()
        keys = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", text, re.M))
        from goa.mock import FAKE_KEYS
        self.assertLessEqual(set(FAKE_KEYS) | {"GOA_LLM", "GOA_DECISION", "GOA_OPENROUTER_DECISION_MODEL"}, keys)
        with open(os.path.join(ROOT, ".gitignore")) as f:
            ignored = f.read().split()
        self.assertIn(".env", ignored)
