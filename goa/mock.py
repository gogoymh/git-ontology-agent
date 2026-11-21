"""Simulated provider APIs: run the real online code path with no key and no network.

`session()` swaps `goa.http.transport` for a router that answers every documented request shape
(Chat Completions, Anthropic Messages, Gemini generateContent, Jev, OpenAI Decisions, Workers AI)
the way a real model would: it reads the prompt the agent built, does the work with the built-in
demo skills, and replies in that provider's JSON. Everything above the transport (request builders,
reply parsers, LLMSkill, Responder, decision normalization) is the production code.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
from collections import Counter
from typing import Any, Dict, Iterator, List, Tuple
from urllib.parse import urlparse

from . import http, log
from .decision import SYSTEM as DECISION_SYSTEM
from .decision import LocalDecision
from .llm import Offline
from .ontology import State
from .respond import Responder
from .skills import CalendarSkill, TripSkill

_log = log.get("mock")

FAKE_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "XAI_API_KEY", "LLAMA_API_KEY",
             "OPENROUTER_API_KEY", "TYPESAFE_API_KEY", "AIMLAPI_API_KEY", "CLOUDFLARE_ACCOUNT_ID",
             "CLOUDFLARE_API_TOKEN")


class Simulator:
    """A stand-in for every provider. `calls` records (provider, endpoint, role) per request."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str, str]] = []
        self.judge = LocalDecision()

    # --- transport ------------------------------------------------------------
    def __call__(self, url: str, headers: Dict[str, str], body: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        u = urlparse(url)
        host, path = u.netloc, u.path
        endpoint = host + path
        if not any(k.lower() in ("authorization", "x-api-key", "x-goog-api-key") for k in headers):
            raise http.HTTPError(401, "missing credentials")
        if path.endswith("/chat/completions"):
            system, prompt = _chat(body)
            text, role = self.think(system, prompt)
            provider = {"api.openai.com": "openai", "api.x.ai": "xai", "api.llama.com": "meta",
                        "openrouter.ai": "openrouter"}.get(host, host)
            reply: Dict[str, Any] = {"choices": [{"message": {"role": "assistant", "content": text}}]}
            if host == "api.llama.com":
                reply = {"completion_message": {"role": "assistant", "content": {"type": "text", "text": text}}}
        elif host == "api.anthropic.com":
            text, role = self.think(body.get("system", ""), body["messages"][-1]["content"])
            provider, reply = "anthropic", {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}
        elif host == "generativelanguage.googleapis.com":
            system = body["systemInstruction"]["parts"][0]["text"]
            text, role = self.think(system, body["contents"][-1]["parts"][0]["text"])
            provider, reply = "gemini", {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}}]}
        elif host == "api.cloudflare.com":
            system, prompt = _chat(body)
            data = json.loads(prompt)
            provider, role = "cloudflare", "decision"
            reply = {"success": True, "result": {"response": self.decide(data["state"], data["questions"])}}
        elif path.endswith("/v1/decisions") and host == "api.openai.com":
            provider, role, reply = "openai", "decision", self._openai_decisions(body)
        elif path.endswith("/decisions") or host == "api.typesafe.ai" or path.endswith("/systemone"):
            provider = {"openrouter.ai": "openrouter", "api.aimlapi.com": "aimlapi"}.get(host, "typesafe")
            role = "decision"
            answers = self.decide(body["state"], body["questions"])
            reply = {"model": body.get("model"),
                     "answers": {q: {"type": body["questions"][q]["type"], **a} for q, a in answers.items()}}
        else:
            raise http.HTTPError(404, f"no simulated provider for {endpoint}")
        self.calls.append((provider, endpoint, role))
        _log.info("simulated %s %s (%s)", provider, endpoint, role)
        return reply

    # --- the "model" ------------------------------------------------------------
    def think(self, system: str, prompt: str) -> Tuple[str, str]:
        """Answer a chat prompt the way the agent's prompts ask; returns (text, role)."""
        lang = _lang(system)
        if system.startswith(DECISION_SYSTEM[:30]):
            data = json.loads(prompt)
            return json.dumps(self.decide(data["state"], data["questions"])), "decision"
        if system.startswith("You are the worker"):
            if "\nNew message: " in prompt:
                return json.dumps(self._interpret(prompt, lang), ensure_ascii=False), "interpret"
            return json.dumps(self._step(prompt, lang), ensure_ascii=False), "work"
        if system.startswith("Write a short chat answer"):
            return Responder(Offline(), lang).template(self._read(json.loads(prompt), lang)), "answer"
        return "", "unknown"

    def _read(self, facts: Dict[str, Any], lang: str) -> Dict[str, Any]:
        """What a model does with the facts before writing: names a trip plan as one, not by its raw text."""
        trip = TripSkill(lang)
        name = lambda t: trip.title(t) if isinstance(t, str) and trip.owns(t) else t  # noqa: E731
        for k in ("still_working",):
            facts[k] = [name(t) for t in facts.get(k) or []]
        for k in ("stopped", "target"):
            if k in facts:
                facts[k] = name(facts[k])
        if facts.get("kind") == "final":
            facts["is_plan"] = facts.get("is_plan") or any(trip.owns(r) for r in facts.get("requests") or [])
        return facts

    def decide(self, state: Any, questions: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        out = self.judge._decide(state, questions)
        st = state if isinstance(state, dict) else {}
        for name, q in questions.items():
            a = out.get(name, {})
            if q["type"] != "choice":
                continue
            if name == "target":  # the request the new message shares the most words with
                words = set(re.findall(r"\w+", str(st.get("new", "")).lower()))
                a["choice"] = max(q["criteria"], key=lambda k: (len(words & set(re.findall(r"\w+", q["criteria"][k].lower()))), k))
            rest = (1 - a["confidence"]) / max(1, len(q["criteria"]) - 1)
            a["probabilities"] = {k: round(a["confidence"] if k == a["choice"] else rest, 4) for k in q["criteria"]}
        return out

    def _openai_decisions(self, body: Dict[str, Any]) -> Dict[str, Any]:
        try:
            state = json.loads(body["input"])
        except ValueError:
            state = body["input"]
        questions = {q["name"]: ({"type": "choice", "instructions": q["instructions"],
                                  "criteria": {c["value"]: c["description"] for c in q["choices"]}}
                                 if q["type"] == "choice" else {"type": "noul", "instructions": q["instructions"]})
                     for q in body["questions"]}
        answers = []
        for name, a in self.decide(state, questions).items():
            if questions[name]["type"] == "choice":
                answers.append({"name": name, "type": "choice", "value": a["choice"],
                                "probabilities": [{"value": k, "probability": p} for k, p in a["probabilities"].items()]})
            else:
                answers.append({"name": name, "type": "predicate", "probability": a["noul"]})
        return {"object": "decision", "model": body.get("model"), "answers": answers}

    def _interpret(self, prompt: str, lang: str) -> Dict[str, Any]:
        state = State.from_yaml(_between(prompt, "Current claims:\n", "\nNew message: "))
        text = prompt.split("\nNew message: ", 1)[1].split("\n", 1)[0]
        return {"claims": [_dict(c) for c in TripSkill(lang).interpret(text, "x", state, 0)]}

    def _step(self, prompt: str, lang: str) -> Dict[str, Any]:
        state = State.from_yaml(_between(prompt, "Current claims:\n", "\nDo the next"))
        reqs = re.findall(r"^- (req\.\d+): (.*)$", _between(prompt, "Requests:\n", "\nCurrent claims:"), re.M)
        newest = max(reqs, key=lambda r: int(r[0].split(".")[1]))[1] if reqs else ""
        skill = CalendarSkill(lang) if CalendarSkill(lang).owns(newest) and not TripSkill(lang).owns(newest) \
            else TripSkill(lang)
        claims = skill.next_step(state, "x", 0) or []
        for c in claims:
            state.put(c)
        return {"done": not claims, "claims": [_dict(c) for c in claims], "remaining": skill.pending(state)}


def _chat(body: Dict[str, Any]) -> Tuple[str, str]:
    msgs = body.get("messages") or []
    system = next((m["content"] for m in msgs if m["role"] == "system"), "")
    return system, msgs[-1]["content"] if msgs else ""


def _lang(system: str) -> str:
    m = re.search(r"language '([^']+)'", system)
    return m.group(1) if m else "en"


def _between(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[-1].split(end, 1)[0]


def _dict(c) -> Dict[str, Any]:
    return {"id": c.id, "type": c.type, "value": str(c.value), "label": c.label, "depends_on": list(c.depends_on)}


def summary(calls: List[Tuple[str, str, str]]) -> str:
    """`anthropic ×4 · typesafe ×1` for a slice of recorded calls."""
    return " · ".join(f"{p} ×{n}" for p, n in Counter(p for p, _, _ in calls).items())


@contextlib.contextmanager
def session() -> Iterator[Simulator]:
    """Simulate every provider until the block ends; fake keys are set only where none exist."""
    sim, orig = Simulator(), http.transport
    added = [k for k in FAKE_KEYS if not os.environ.get(k)]
    for k in added:
        os.environ[k] = "mock-" + k.lower()
    http.transport = sim
    _log.info("mock session on (%d fake keys)", len(added))
    try:
        yield sim
    finally:
        http.transport = orig
        for k in added:
            os.environ.pop(k, None)
        _log.info("mock session off after %d simulated calls", len(sim.calls))
