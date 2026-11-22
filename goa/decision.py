"""Decision models: typed answers (choice / noul) with calibrated confidence.

Canonical question format is the Jev shape:
    {"relation": {"type": "choice", "instructions": "...", "criteria": {"k1": "...", ...}},
     "intent":   {"type": "noul",   "instructions": "..."}}
Canonical answers:
    {"relation": {"type": "choice", "choice": "k1", "confidence": 0.94, "probabilities": {...}},
     "intent":   {"type": "noul", "noul": 0.81}}

Backends: Typesafe Jev, OpenAI Decisions, Gemini (structured output), Cloudflare Workers AI,
any OpenRouter chat model (strict JSON schema), any Decisions gateway (OpenRouter / AIML API: Jev,
cloudflare/clef, ...),
any LLM from goa.llm, and an offline heuristic.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Optional

from . import http, log
from .llm import LLM, gemini_schema, make_llm, parse_json

_log = log.get("decision")

Questions = Dict[str, Dict[str, Any]]
Answers = Dict[str, Dict[str, Any]]


class DecisionModel:
    name = "local/heuristic"

    def decide(self, state: Any, questions: Questions) -> Answers:
        answers = self._decide(state, questions)
        for q, spec in questions.items():
            answers[q] = _normalize(spec, answers.get(q, {}))
        _log.info("%s decided %s", self.name,
                  {q: a.get("choice", a.get("noul")) for q, a in answers.items()})
        return answers

    def _decide(self, state: Any, questions: Questions) -> Answers:
        raise NotImplementedError


def _normalize(spec: Dict[str, Any], ans: Dict[str, Any]) -> Dict[str, Any]:
    if spec["type"] == "choice":
        keys = list(spec["criteria"])
        probs = {k: float(v) for k, v in (ans.get("probabilities") or {}).items() if k in keys}
        choice = ans.get("choice")
        if choice not in keys:
            choice = max(probs, key=probs.get) if probs else keys[0]
        conf = ans.get("confidence")
        conf = float(conf) if conf is not None else probs.get(choice, 0.0)
        return {"type": "choice", "choice": choice, "confidence": max(0.0, min(1.0, conf)),
                "probabilities": probs or {choice: conf}}
    p = ans.get("noul", ans.get("probability", 0.0))
    return {"type": "noul", "noul": max(0.0, min(1.0, float(p or 0.0)))}


# --- Jev shape over HTTP ---------------------------------------------------------
class JevAPI(DecisionModel):
    """Typesafe's own endpoint, or any gateway that speaks the Jev shape."""

    def __init__(self, name: str, url: str, key_env: str, model: str):
        self.name, self.url, self.key_env, self.model = name, url, key_env, model

    def request(self, state: Any, questions: Questions):
        key = os.environ.get(self.key_env, "")
        if not key:
            raise RuntimeError(f"set {self.key_env} to use {self.name}")
        return self.url, {"Authorization": f"Bearer {key}"}, {"model": self.model, "state": state,
                                                              "questions": questions}

    def _decide(self, state, questions):
        url, headers, body = self.request(state, questions)
        out = http.post_json(url, headers, body)
        return {q: a for q, a in (out.get("answers") or {}).items() if a.get("type") != "refusal"}


# --- OpenAI Decisions shape ------------------------------------------------------
class OpenAIDecisions(DecisionModel):
    """OpenAI Decisions API: `input` text, a `questions` array, predicate/choice types."""

    def __init__(self, model: str, url: str):
        self.model, self.url = model, url
        self.name = f"openai/{model}"

    def request(self, state, questions):
        qs = []
        for name, q in questions.items():
            if q["type"] == "choice":
                qs.append({"name": name, "type": "choice", "instructions": q["instructions"],
                           "choices": [{"value": k, "description": d} for k, d in q["criteria"].items()]})
            else:
                qs.append({"name": name, "type": "predicate", "instructions": q["instructions"]})
        key = os.environ.get("OPENAI_API_KEY", "")
        if not key:
            raise RuntimeError("set OPENAI_API_KEY to use openai decisions")
        body = {"model": self.model, "input": state if isinstance(state, str) else json.dumps(state, ensure_ascii=False),
                "questions": qs}
        return self.url, {"Authorization": f"Bearer {key}"}, body

    def _decide(self, state, questions):
        url, headers, body = self.request(state, questions)
        out = http.post_json(url, headers, body)
        answers: Answers = {}
        for a in out.get("answers") or []:
            name = a.get("name")
            if a.get("type") == "predicate":
                answers[name] = {"noul": a.get("probability", 0.0)}
            elif a.get("type") == "choice":
                probs = {p["value"]: p["probability"] for p in a.get("probabilities") or []}
                answers[name] = {"choice": a.get("value") or (max(probs, key=probs.get) if probs else None),
                                 "probabilities": probs}
        return answers


# --- LLM-backed structured decisions (Gemini, Cloudflare, any LLM) ---------------
SYSTEM = ("You are a decision model. Read the state and answer every question. For a choice, pick "
          "exactly one key from its criteria, judging by the descriptions, not the key names, and give "
          "your confidence in [0,1]. For a noul (yes/no), give the probability of yes in [0,1].")


def _schema(questions: Questions) -> Dict[str, Any]:
    props: Dict[str, Any] = {}
    for name, q in questions.items():
        if q["type"] == "choice":
            props[name] = {"type": "object", "required": ["choice", "confidence"], "properties": {
                "choice": {"type": "string", "enum": list(q["criteria"])},
                "confidence": {"type": "number"}}}
        else:
            props[name] = {"type": "object", "required": ["noul"], "properties": {"noul": {"type": "number"}}}
    return {"type": "object", "required": list(questions), "properties": props}


def _prompt(state, questions) -> str:
    return json.dumps({"state": state, "questions": questions}, ensure_ascii=False, indent=1)


class GeminiDecision(DecisionModel):
    def __init__(self, model: str):
        self.model = model
        self.name = f"gemini/{model}"

    def request(self, state, questions):
        key = os.environ.get("GEMINI_API_KEY", "")
        if not key:
            raise RuntimeError("set GEMINI_API_KEY to use gemini decisions")
        body = {"systemInstruction": {"parts": [{"text": SYSTEM}]},
                "contents": [{"role": "user", "parts": [{"text": _prompt(state, questions)}]}],
                "generationConfig": {"temperature": 0, "responseMimeType": "application/json",
                                     "responseSchema": gemini_schema(_schema(questions))}}
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        return url, {"x-goog-api-key": key}, body

    def _decide(self, state, questions):
        url, headers, body = self.request(state, questions)
        out = http.post_json(url, headers, body)
        text = "".join(p.get("text", "") for p in out["candidates"][0]["content"]["parts"])
        return parse_json(text)


class CloudflareDecision(DecisionModel):
    """Cloudflare Workers AI with JSON-schema mode."""

    def __init__(self, model: str):
        self.model = model
        self.name = f"cloudflare/{model}"

    def request(self, state, questions):
        acct, token = os.environ.get("CLOUDFLARE_ACCOUNT_ID", ""), os.environ.get("CLOUDFLARE_API_TOKEN", "")
        if not (acct and token):
            raise RuntimeError("set CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN to use cloudflare decisions")
        body = {"messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": _prompt(state, questions)}],
                "temperature": 0,
                "response_format": {"type": "json_schema", "json_schema": _schema(questions)}}
        url = f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run/{self.model}"
        return url, {"Authorization": f"Bearer {token}"}, body

    def _decide(self, state, questions):
        url, headers, body = self.request(state, questions)
        out = http.post_json(url, headers, body)
        res = out.get("result", {}).get("response")
        return res if isinstance(res, dict) else parse_json(res or "{}")


class OpenRouterDecision(DecisionModel):
    """Any OpenRouter chat model as a judge: strict JSON-schema output, temperature 0.
    `require_parameters` keeps OpenRouter from routing to a provider that ignores the schema."""

    URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, model: str):
        self.model = model
        self.name = f"openrouter/{model}"

    def request(self, state, questions):
        key = os.environ.get("OPENROUTER_API_KEY", "")
        if not key:
            raise RuntimeError("set OPENROUTER_API_KEY to use openrouter decisions")
        body = {"model": self.model, "temperature": 0, "max_tokens": 400,
                "messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": _prompt(state, questions)}],
                "response_format": {"type": "json_schema", "json_schema": {
                    "name": "decision", "strict": True, "schema": _strict(_schema(questions))}},
                "provider": {"require_parameters": True}}
        headers = {"Authorization": f"Bearer {key}", "X-Title": "Git Ontology Agent"}
        return os.environ.get("OPENROUTER_CHAT_URL") or self.URL, headers, body

    def _decide(self, state, questions):
        url, headers, body = self.request(state, questions)
        out = http.post_json(url, headers, body)
        return parse_json(out["choices"][0]["message"].get("content") or "{}")


def _strict(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Strict mode needs additionalProperties false on every object."""
    if schema.get("type") == "object":
        schema = {**schema, "additionalProperties": False,
                  "properties": {k: _strict(v) for k, v in schema.get("properties", {}).items()}}
    return schema


# Decision-native models OpenRouter serves through its Jev-shape Decisions gateway.
JEV_NATIVE = ("typesafe/", "cloudflare/clef")


class LLMDecision(DecisionModel):
    def __init__(self, llm: LLM):
        self.llm = llm
        self.name = f"llm/{llm.name}"

    def _decide(self, state, questions):
        return self.llm.json(SYSTEM, _prompt(state, questions), _schema(questions), 400)


# --- Offline heuristic -------------------------------------------------------
_CANCEL = r"그만|취소|멈춰|중단|\bstop\b|\bcancel|never ?mind|forget it|drop it"
_PROGRESS = r"어디까지|진행 ?상황|얼마나 됐|how far|progress|status|where are you"
_CORRECT = r"말고|아니라|아니고|바꿔|정정|\binstead\b|\bnot\b.*\bbut\b|\bactually\b|change .* to"
_YES = r"반영|응|네|예|맞아|그래|yes|apply|same|this one|current|진행 중"


class LocalDecision(DecisionModel):
    """Keyword + ontology-hint heuristic. Deterministic, offline, and good enough for demos/tests."""
    name = "local/heuristic"

    def _decide(self, state, questions):
        st = state if isinstance(state, dict) else {"new": str(state)}
        text = str(st.get("new", "")).lower()
        touches = st.get("touches", [])
        out: Answers = {}
        for name, q in questions.items():
            keys = list(q.get("criteria", {}))
            if name == "relation":
                if re.search(_CANCEL, text):
                    k, c = "k4", 0.95
                elif re.search(_PROGRESS, text):
                    k, c = "k5", 0.96
                elif re.search(_CORRECT, text) or "Assumption" in touches:
                    k, c = "k2", 0.91
                elif "Constraint" in touches:
                    k, c = "k1", 0.94
                elif touches:
                    k, c = "k1", 0.82
                else:
                    k, c = "k3", 0.9 if text.rstrip().endswith("?") or "?" in text else 0.6
                out[name] = {"choice": k, "confidence": c}
            elif name == "clarify":
                out[name] = {"choice": keys[0] if re.search(_YES, text) else keys[-1], "confidence": 0.9}
            elif q["type"] == "choice":
                out[name] = {"choice": st.get("target_hint") or keys[0], "confidence": 0.85}
            else:
                out[name] = {"noul": 0.5}
        return out


# --- factory -----------------------------------------------------------------
PROVIDERS = ("local", "typesafe", "openai", "gemini", "cloudflare", "openrouter", "aimlapi", "llm")


def make_decision(spec: str = "local") -> DecisionModel:
    """`typesafe`, `typesafe:typesafe/jev-1.13`, `openrouter:openai/gpt-5.1`, `openrouter:cloudflare/clef`,
    `llm:anthropic`, ..."""
    provider, _, model = (spec or "local").partition(":")
    provider = provider.lower()
    env = lambda k, d: os.environ.get(k) or d  # noqa: E731
    if provider == "local":
        return LocalDecision()
    if provider == "typesafe":
        m = model or env("GOA_TYPESAFE_MODEL", "typesafe/jev-1.14")
        return JevAPI(m, env("TYPESAFE_URL", "https://api.typesafe.ai/v1/systemone"), "TYPESAFE_API_KEY", m)
    if provider == "openrouter":
        m = model or env("GOA_OPENROUTER_DECISION_MODEL", "openrouter/auto")
        if m.startswith(JEV_NATIVE):
            return JevAPI(f"openrouter/{m}", env("OPENROUTER_DECISIONS_URL", "https://openrouter.ai/api/alpha/decisions"),
                          "OPENROUTER_API_KEY", m)
        return OpenRouterDecision(m)
    if provider == "aimlapi":
        m = model or "typesafe/jev-1.14"
        return JevAPI(f"aimlapi/{m}", "https://api.aimlapi.com/v1/decisions", "AIMLAPI_API_KEY", m)
    if provider == "openai":
        return OpenAIDecisions(model or env("GOA_OPENAI_DECISION_MODEL", "gpt-6-luna"),
                               env("OPENAI_DECISIONS_URL", "https://api.openai.com/v1/decisions"))
    if provider == "gemini":
        return GeminiDecision(model or env("GOA_GEMINI_DECISION_MODEL", "gemini-2.5-flash"))
    if provider == "cloudflare":
        return CloudflareDecision(model or env("GOA_CLOUDFLARE_MODEL", "@cf/meta/llama-3.3-70b-instruct-fp8-fast"))
    if provider == "llm":
        return LLMDecision(make_llm(model or "openai"))
    raise ValueError(f"unknown decision provider {provider!r}; pick one of {', '.join(PROVIDERS)}")


def noul(model: DecisionModel, state: Any, instructions: str) -> float:
    return model.decide(state, {"q": {"type": "noul", "instructions": instructions}})["q"]["noul"]


def choice(model: DecisionModel, state: Any, instructions: str, criteria: Dict[str, str],
           name: str = "q") -> Optional[Dict[str, Any]]:
    return model.decide(state, {name: {"type": "choice", "instructions": instructions, "criteria": criteria}})[name]
