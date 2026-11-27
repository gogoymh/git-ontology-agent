"""Tests for keyless loopback endpoints, cyclic dependency safety, and probed claim reuse."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from goa import Agent
from goa.decision import JevAPI, OpenAIDecisions, make_decision
from goa.llm import LLM, OpenAICompatible, make_llm
from goa.ontology import ASSUMPTION, CONFIRMED, DECISION, INVALID, Claim, State


class _CountingLLM(LLM):
    name, offline = "counting", False

    def __init__(self):
        self.interpret_calls = 0

    def complete(self, system, prompt, schema=None, max_tokens=1024):
        if schema is None:
            return "ok"
        if "Return only the Constraint" in prompt:
            self.interpret_calls += 1
            return json.dumps({"claims": [
                {"id": "constraint.budget", "type": "Constraint", "value": "500k",
                 "label": "budget", "depends_on": []}]})
        return json.dumps({"done": False, "remaining": ["hotel"], "claims": [
            {"id": "plan.train", "type": "Decision", "value": "KTX",
             "label": "train", "depends_on": []}]})


class GraphAndProbeReuseTests(unittest.TestCase):
    def test_cyclic_dependency_does_not_invalidate_origin_claim(self):
        s = State([
            Claim("a", ASSUMPTION, "1", "req.1", depends_on=["b"]),
            Claim("b", DECISION, "2", "req.1", depends_on=["a"]),
        ])
        inv = s.invalidate(["a"])
        self.assertEqual(inv, ["b"])
        claim_a = s.get("a")
        claim_b = s.get("b")
        assert claim_a is not None and claim_b is not None
        self.assertEqual(claim_a.status, CONFIRMED)
        self.assertEqual(claim_b.status, INVALID)

    def test_followup_reuses_probed_interpret_without_duplicate_llm_call(self):
        d = tempfile.mkdtemp(prefix="goa-cache-")
        try:
            llm = _CountingLLM()
            a = Agent(d, llm=llm, lang="en")
            a.submit("Plan trip")
            after_r1 = llm.interpret_calls
            with self.assertLogs("goa.agent", level="DEBUG") as cm:
                a.submit("keep budget under 500k")
            self.assertEqual(llm.interpret_calls - after_r1, 1)
            self.assertTrue(any("reusing" in line and "probed claims" in line for line in cm.output))
        finally:
            shutil.rmtree(d, ignore_errors=True)


class KeylessLoopbackTests(unittest.TestCase):
    def test_local_loopback_endpoints_allow_keyless_requests(self):
        with mock.patch.dict(os.environ, {
            "TYPESAFE_URL": "http://127.0.0.1:8888/v1/systemone",
            "TYPESAFE_API_KEY": "",
            "OPENAI_BASE_URL": "http://127.0.0.1:18426/v1",
            "OPENAI_DECISIONS_URL": "http://localhost:18426/v1/decisions",
            "OPENAI_API_KEY": "",
        }):
            dm = make_decision("typesafe:local-clef")
            assert isinstance(dm, JevAPI)
            with self.assertLogs("goa.decision", level="INFO") as dm_logs:
                url, headers, body = dm.request({"new": "hi"}, {"r": {"type": "noul", "instructions": "ok?"}})
            self.assertEqual(url, "http://127.0.0.1:8888/v1/systemone")
            self.assertNotIn("Authorization", headers)
            self.assertEqual(body["model"], "local-clef")
            self.assertTrue(any("keyless loopback" in line for line in dm_logs.output))

            odm = make_decision("openai:local-luna")
            assert isinstance(odm, OpenAIDecisions)
            with self.assertLogs("goa.decision", level="INFO") as odm_logs:
                ourl, oheaders, obody = odm.request({"new": "hi"}, {"r": {"type": "noul", "instructions": "ok?"}})
            self.assertEqual(ourl, "http://localhost:18426/v1/decisions")
            self.assertNotIn("Authorization", oheaders)
            self.assertEqual(obody["model"], "local-luna")
            self.assertTrue(any("keyless loopback" in line for line in odm_logs.output))

            llm = make_llm("openai:local-model")
            assert isinstance(llm, OpenAICompatible)
            with self.assertLogs("goa.llm", level="INFO") as llm_logs:
                lurl, lheaders, lbody = llm.request("sys", "hi")
            self.assertEqual(lurl, "http://127.0.0.1:18426/v1/chat/completions")
            self.assertNotIn("Authorization", lheaders)
            self.assertEqual(lbody["model"], "local-model")
            self.assertTrue(any("keyless loopback" in line for line in llm_logs.output))

    def test_remote_endpoints_still_require_api_key(self):
        with mock.patch.dict(os.environ, {
            "TYPESAFE_URL": "https://api.typesafe.example/v1/systemone",
            "TYPESAFE_API_KEY": "",
            "OPENAI_BASE_URL": "https://api.openai.com/v1",
            "OPENAI_DECISIONS_URL": "https://api.openai.com/v1/decisions",
            "OPENAI_API_KEY": "",
        }):
            dm = make_decision("typesafe:jev")
            assert isinstance(dm, JevAPI)
            with self.assertRaises(RuntimeError):
                dm.request({"new": "hi"}, {"r": {"type": "noul", "instructions": "ok?"}})

            odm = make_decision("openai:gpt-6-luna")
            assert isinstance(odm, OpenAIDecisions)
            with self.assertRaises(RuntimeError):
                odm.request({"new": "hi"}, {"r": {"type": "noul", "instructions": "ok?"}})

            llm = make_llm("openai:gpt-5.1")
            assert isinstance(llm, OpenAICompatible)
            with self.assertRaises(RuntimeError):
                llm.request("sys", "hi")


if __name__ == "__main__":
    unittest.main()
