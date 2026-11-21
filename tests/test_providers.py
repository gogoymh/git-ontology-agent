"""Every LLM and decision backend builds the documented request and parses the documented reply.
No network: `goa.http.transport` is replaced by a recorder."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from goa import Agent, http
from goa.decision import make_decision
from goa.llm import make_llm

KEYS = {"OPENAI_API_KEY": "sk-o", "ANTHROPIC_API_KEY": "sk-a", "GEMINI_API_KEY": "g-k", "XAI_API_KEY": "x-k",
        "LLAMA_API_KEY": "m-k", "OPENROUTER_API_KEY": "or-k", "TYPESAFE_API_KEY": "ts-k",
        "CLOUDFLARE_ACCOUNT_ID": "acct", "CLOUDFLARE_API_TOKEN": "cf-t", "AIMLAPI_API_KEY": "ai-k"}
RELATION = {"relation": {"type": "choice", "instructions": "How does the new request relate?",
                         "criteria": {"k1": "adds", "k2": "changes", "k3": "separate", "k4": "stop", "k5": "status"}},
            "intent": {"type": "noul", "instructions": "Does it change the intent?"}}


class Recorder:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, headers, json.loads(json.dumps(body))))
        return self.reply


class ProviderTest(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, KEYS)
        self.env.start()
        self.orig = http.transport

    def tearDown(self):
        http.transport = self.orig
        self.env.stop()

    def use(self, reply):
        http.transport = Recorder(reply)
        return http.transport


OPENAI_REPLY = {"choices": [{"message": {"content": "hello"}}]}


class LLMs(ProviderTest):
    def check_openai_shape(self, spec, url, auth, model):
        rec = self.use(OPENAI_REPLY)
        llm = make_llm(spec)
        self.assertEqual(llm.complete("sys", "hi", {"type": "object"}), "hello")
        u, h, b = rec.calls[0]
        self.assertEqual(u, url)
        self.assertEqual(h["Authorization"], f"Bearer {auth}")
        self.assertEqual(b["model"], model)
        self.assertEqual(b["messages"][0], {"role": "system", "content": "sys"})
        self.assertEqual(b["response_format"]["type"], "json_schema")

    def test_openai(self):
        self.check_openai_shape("openai", "https://api.openai.com/v1/chat/completions", "sk-o", "gpt-5.1")
        self.assertIn("max_completion_tokens", http.transport.calls[0][2])

    def test_xai(self):
        self.check_openai_shape("xai", "https://api.x.ai/v1/chat/completions", "x-k", "grok-4")

    def test_meta(self):
        self.check_openai_shape("meta", "https://api.llama.com/compat/v1/chat/completions", "m-k",
                                "Llama-4-Maverick-17B-128E-Instruct-FP8")
        self.use({"completion_message": {"content": {"type": "text", "text": "native"}}})
        self.assertEqual(make_llm("meta").complete("s", "p"), "native")

    def test_openrouter(self):
        self.check_openai_shape("openrouter:meta-llama/llama-4-maverick", "https://openrouter.ai/api/v1/chat/completions",
                                "or-k", "meta-llama/llama-4-maverick")

    def test_anthropic(self):
        rec = self.use({"content": [{"type": "text", "text": "hi "}, {"type": "text", "text": "there"}]})
        self.assertEqual(make_llm("anthropic:claude-opus-5-5").complete("sys", "p", {"type": "object"}), "hi there")
        u, h, b = rec.calls[0]
        self.assertEqual(u, "https://api.anthropic.com/v1/messages")
        self.assertEqual((h["x-api-key"], h["anthropic-version"]), ("sk-a", "2023-06-01"))
        self.assertEqual((b["model"], b["messages"][0]["role"]), ("claude-opus-5-5", "user"))
        self.assertIn("JSON Schema", b["system"])

    def test_gemini(self):
        rec = self.use({"candidates": [{"content": {"parts": [{"text": '{"a": 1}'}]}}]})
        self.assertEqual(make_llm("gemini").json("sys", "p", {"type": "object", "properties": {"a": {"type": "number"}}}),
                         {"a": 1})
        u, h, b = rec.calls[0]
        self.assertEqual(u, "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent")
        self.assertEqual(h["x-goog-api-key"], "g-k")
        self.assertEqual(b["generationConfig"]["responseSchema"]["properties"]["a"]["type"], "NUMBER")

    def test_missing_key_is_explained(self):
        with mock.patch.dict(os.environ, {"XAI_API_KEY": ""}):
            with self.assertRaisesRegex(RuntimeError, "XAI_API_KEY"):
                make_llm("xai").complete("s", "p")

    def test_unknown_provider(self):
        with self.assertRaises(ValueError):
            make_llm("nope")


JEV_REPLY = {"model": "jev-1.14.0", "answers": {
    "relation": {"type": "choice", "choice": "k1", "confidence": 0.94,
                 "probabilities": {"k1": 0.94, "k2": 0.03, "k3": 0.02, "k4": 0.0, "k5": 0.01}},
    "intent": {"type": "noul", "noul": 0.81}}}


class Decisions(ProviderTest):
    def check(self, answers):
        self.assertEqual(answers["relation"]["choice"], "k1")
        self.assertAlmostEqual(answers["relation"]["confidence"], 0.94)
        self.assertAlmostEqual(answers["intent"]["noul"], 0.81)

    def test_typesafe_jev(self):
        rec = self.use(JEV_REPLY)
        m = make_decision("typesafe")
        self.assertEqual(m.name, "typesafe/jev-1.14")
        self.check(m.decide({"in_progress": "R1", "new": "budget"}, RELATION))
        u, h, b = rec.calls[0]
        self.assertEqual(u, "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(h["Authorization"], "Bearer ts-k")
        self.assertEqual(b, {"model": "typesafe/jev-1.14", "state": {"in_progress": "R1", "new": "budget"},
                             "questions": RELATION})

    def test_gateways_carry_jev_and_clef(self):
        for spec, url, model in (("openrouter:cloudflare/clef", "https://openrouter.ai/api/alpha/decisions", "cloudflare/clef"),
                                 ("aimlapi", "https://api.aimlapi.com/v1/decisions", "typesafe/jev-1.14")):
            rec = self.use(JEV_REPLY)
            self.check(make_decision(spec).decide("state", RELATION))
            self.assertEqual((rec.calls[0][0], rec.calls[0][2]["model"]), (url, model))

    def test_refusal_falls_back_safely(self):
        self.use({"answers": {"relation": {"type": "refusal"}, "intent": {"type": "noul", "noul": 0.81}}})
        a = make_decision("typesafe").decide("s", RELATION)
        self.assertEqual(a["relation"]["confidence"], 0.0)   # forces a clarifying question

    def test_openai_decisions(self):
        rec = self.use({"answers": [
            {"name": "relation", "type": "choice", "value": "k1",
             "probabilities": [{"value": "k1", "probability": 0.94}, {"value": "k3", "probability": 0.06}]},
            {"name": "intent", "type": "predicate", "probability": 0.81}]})
        m = make_decision("openai")
        self.assertEqual(m.name, "openai/gpt-6-luna")
        self.check(m.decide({"new": "budget"}, RELATION))
        u, h, b = rec.calls[0]
        self.assertEqual(u, "https://api.openai.com/v1/decisions")
        self.assertEqual(h["Authorization"], "Bearer sk-o")
        self.assertEqual(json.loads(b["input"]), {"new": "budget"})
        rel, intent = b["questions"]
        self.assertEqual((rel["name"], rel["type"], rel["choices"][0]), ("relation", "choice", {"value": "k1", "description": "adds"}))
        self.assertEqual((intent["name"], intent["type"]), ("intent", "predicate"))

    def test_gemini_structured(self):
        rec = self.use({"candidates": [{"content": {"parts": [{"text": json.dumps(
            {"relation": {"choice": "k1", "confidence": 0.94}, "intent": {"noul": 0.81}})}]}}]})
        m = make_decision("gemini")
        self.check(m.decide("s", RELATION))
        u, h, b = rec.calls[0]
        self.assertTrue(u.endswith("/models/gemini-2.5-flash:generateContent"))
        schema = b["generationConfig"]["responseSchema"]
        self.assertEqual(schema["properties"]["relation"]["properties"]["choice"]["enum"], ["k1", "k2", "k3", "k4", "k5"])
        self.assertEqual(b["generationConfig"]["temperature"], 0)

    def test_cloudflare_workers_ai(self):
        rec = self.use({"success": True, "result": {"response": {"relation": {"choice": "k1", "confidence": 0.94},
                                                                 "intent": {"noul": 0.81}}}})
        self.check(make_decision("cloudflare").decide("s", RELATION))
        u, h, b = rec.calls[0]
        self.assertEqual(u, "https://api.cloudflare.com/client/v4/accounts/acct/ai/run/@cf/meta/llama-3.3-70b-instruct-fp8-fast")
        self.assertEqual(h["Authorization"], "Bearer cf-t")
        self.assertEqual(b["response_format"]["type"], "json_schema")
        # string responses are parsed too
        self.use({"result": {"response": '{"relation": {"choice": "k1", "confidence": 0.94}, "intent": {"noul": 0.81}}'}})
        self.check(make_decision("cloudflare").decide("s", RELATION))

    def test_any_llm_as_decision_model(self):
        self.use({"content": [{"type": "text", "text": '{"relation": {"choice": "k1", "confidence": 0.94}, "intent": {"noul": 0.81}}'}]})
        m = make_decision("llm:anthropic")
        self.assertEqual(m.name, "llm/anthropic")
        self.check(m.decide("s", RELATION))

    def test_out_of_range_values_are_clamped(self):
        self.use({"answers": {"relation": {"choice": "zz", "probabilities": {"k2": 0.7, "k1": 0.2}},
                              "intent": {"noul": 1.7}}})
        a = make_decision("typesafe").decide("s", RELATION)
        self.assertEqual((a["relation"]["choice"], a["intent"]["noul"]), ("k2", 1.0))


class AgentWithJev(ProviderTest):
    def test_relation_question_matches_spec(self):
        rec = self.use(JEV_REPLY)
        d = tempfile.mkdtemp(prefix="goa-test-")
        try:
            a = Agent(d, decision="typesafe", lang="en")
            a.submit("Plan my Busan business trip next week"); a.tick()
            [i] = a.submit("Oh, keep the budget under 500,000 won")
            self.assertEqual(i.relation, "k1")
            body = rec.calls[0][2]
            self.assertEqual(body["model"], "typesafe/jev-1.14")
            self.assertEqual(set(body["state"]), {"in_progress", "new", "touches"})
            self.assertEqual(body["state"]["touches"], ["Constraint"])
            q = body["questions"]["relation"]
            self.assertEqual((q["type"], list(q["criteria"])), ("choice", ["k1", "k2", "k3", "k4", "k5"]))
            self.assertIn("Decision-Model: typesafe/jev-1.14", a.repo.git("log", "-1", "--format=%B", "req/1"))
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()


class ScriptedLLM:
    """Stands in for any online provider to drive the generic LLM skill end to end."""
    name, offline = "scripted", False

    def __init__(self):
        self.steps = 0

    def complete(self, system, prompt, schema=None, max_tokens=1024):
        if schema is None:                                   # answer writing
            return "LLM answer: " + json.loads(prompt)["kind"]
        if "Return only the Constraint" in prompt:           # interpret
            claims = [{"id": "constraint.lang", "type": "Constraint", "value": "Python", "label": "language",
                       "depends_on": []}] if "Python" in prompt.split("New message:")[1] else []
            return json.dumps({"claims": claims})
        self.steps += 1                                      # work step
        if self.steps == 1:
            return json.dumps({"done": False, "remaining": ["code"], "claims": [
                {"id": "plan.outline", "type": "Decision", "value": "3 parts", "label": "outline", "depends_on": ["constraint.*"]}]})
        if self.steps == 2:
            return json.dumps({"done": False, "remaining": [], "claims": [
                {"id": "plan.outline", "type": "Decision", "value": "3 parts in Python", "label": "outline",
                 "depends_on": ["constraint.*"]}]})
        return json.dumps({"done": True, "remaining": [], "claims": []})

    def json(self, system, prompt, schema, max_tokens=1024):
        return json.loads(self.complete(system, prompt, schema, max_tokens))


class GenericLLMSkill(unittest.TestCase):
    def test_llm_skill_flow(self):
        d = tempfile.mkdtemp(prefix="goa-test-")
        try:
            a = Agent(d, llm=ScriptedLLM(), lang="en")
            a.submit("Write a tutorial"); a.tick()
            [i] = a.submit("Use Python please")
            self.assertEqual((i.relation, i.text), ("k1", "LLM answer: supplement"))
            self.assertEqual(a.repo.read_state("req/1").get("plan.outline").status, "invalid")
            [final] = a.run()
            self.assertEqual(final.text, "LLM answer: final")
            main = a.repo.read_state("main")
            self.assertEqual(main.get("plan.outline").value, "3 parts in Python")
            self.assertIsNone(main.get("progress.remaining"))
        finally:
            shutil.rmtree(d, ignore_errors=True)
