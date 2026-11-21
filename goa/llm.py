"""LLM providers: OpenAI, Anthropic, Gemini, xAI, Meta (Llama API), OpenRouter, and offline.

Spec strings look like `openai`, `anthropic:claude-opus-5-5` or `openrouter:meta-llama/llama-4-maverick`.
Keys come from the usual environment variables; models can be overridden with GOA_<PROVIDER>_MODEL.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Optional

from . import http, log

_log = log.get("llm")


class LLM:
    name = "offline"
    offline = True

    def complete(self, system: str, prompt: str, schema: Optional[Dict[str, Any]] = None,
                 max_tokens: int = 1024) -> str:
        raise NotImplementedError

    def json(self, system: str, prompt: str, schema: Dict[str, Any], max_tokens: int = 1024) -> Any:
        return parse_json(self.complete(system, prompt, schema, max_tokens))


class Offline(LLM):
    """No network. The agent falls back to built-in skills and templates."""

    def complete(self, system, prompt, schema=None, max_tokens=1024):
        return ""


def parse_json(text: str) -> Any:
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}|\[.*\]", text, re.S)
        if not m:
            raise ValueError(f"no JSON in model output: {text[:200]!r}")
        return json.loads(m.group(0))


def _key(env: str) -> str:
    v = os.environ.get(env, "")
    if not v:
        raise RuntimeError(f"set {env} to use this provider")
    return v


class OpenAICompatible(LLM):
    """Chat Completions shape, shared by OpenAI, xAI, Meta Llama API and OpenRouter."""
    offline = False

    def __init__(self, name: str, base_url: str, key_env: str, model: str, extra_headers=None):
        self.name, self.base_url, self.key_env, self.model = name, base_url.rstrip("/"), key_env, model
        self.extra_headers = extra_headers or {}

    def request(self, system, prompt, schema=None, max_tokens=1024):
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        }
        if self.name == "openai":
            body["max_completion_tokens"] = body.pop("max_tokens")
        if schema:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "goa", "schema": schema}}
        headers = {"Authorization": f"Bearer {_key(self.key_env)}", **self.extra_headers}
        return f"{self.base_url}/chat/completions", headers, body

    def complete(self, system, prompt, schema=None, max_tokens=1024):
        url, headers, body = self.request(system, prompt, schema, max_tokens)
        out = http.post_json(url, headers, body)
        msg = out["choices"][0]["message"]
        content = msg.get("content")
        if isinstance(content, list):  # Meta returns typed parts
            content = "".join(p.get("text", "") for p in content)
        if isinstance(content, dict):
            content = content.get("text", "")
        _log.debug("%s/%s returned %d chars", self.name, self.model, len(content or ""))
        return content or ""


class MetaLlama(OpenAICompatible):
    """Meta Llama API (api.llama.com). Native responses carry `completion_message`."""

    def complete(self, system, prompt, schema=None, max_tokens=1024):
        url, headers, body = self.request(system, prompt, schema, max_tokens)
        out = http.post_json(url, headers, body)
        if "completion_message" in out:
            c = out["completion_message"].get("content", {})
            return c.get("text", "") if isinstance(c, dict) else str(c)
        return out["choices"][0]["message"]["content"] or ""


class Anthropic(LLM):
    name, offline = "anthropic", False

    def __init__(self, model: str):
        self.model = model

    def complete(self, system, prompt, schema=None, max_tokens=1024):
        if schema:
            system += "\n\nReply with one JSON object only, matching this JSON Schema:\n" + json.dumps(schema)
        body = {"model": self.model, "max_tokens": max_tokens, "system": system,
                "messages": [{"role": "user", "content": prompt}]}
        headers = {"x-api-key": _key("ANTHROPIC_API_KEY"), "anthropic-version": "2023-06-01"}
        out = http.post_json("https://api.anthropic.com/v1/messages", headers, body)
        return "".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text")


def gemini_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """JSON Schema -> Gemini OpenAPI subset (uppercase types, no additionalProperties)."""
    out: Dict[str, Any] = {}
    for k, v in schema.items():
        if k == "type":
            out[k] = str(v).upper()
        elif k == "properties":
            out[k] = {p: gemini_schema(s) for p, s in v.items()}
        elif k == "items":
            out[k] = gemini_schema(v)
        elif k in ("enum", "required", "description", "minimum", "maximum"):
            out[k] = v
    return out


class Gemini(LLM):
    name, offline = "gemini", False

    def __init__(self, model: str):
        self.model = model

    def request(self, system, prompt, schema=None, max_tokens=1024):
        cfg: Dict[str, Any] = {"maxOutputTokens": max_tokens}
        if schema:
            cfg["responseMimeType"] = "application/json"
            cfg["responseSchema"] = gemini_schema(schema)
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": cfg}
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        return url, {"x-goog-api-key": _key("GEMINI_API_KEY")}, body

    def complete(self, system, prompt, schema=None, max_tokens=1024):
        url, headers, body = self.request(system, prompt, schema, max_tokens)
        out = http.post_json(url, headers, body)
        parts = out["candidates"][0]["content"].get("parts", [])
        return "".join(p.get("text", "") for p in parts)


DEFAULT_MODELS = {
    "openai": "gpt-5.1",
    "anthropic": "claude-sonnet-5-5",
    "gemini": "gemini-2.5-flash",
    "xai": "grok-4",
    "meta": "Llama-4-Maverick-17B-128E-Instruct-FP8",
    "openrouter": "openrouter/auto",
}
PROVIDERS = tuple(DEFAULT_MODELS) + ("offline",)


def make_llm(spec: str = "offline") -> LLM:
    provider, _, model = (spec or "offline").partition(":")
    provider = provider.lower()
    if provider == "offline":
        return Offline()
    if provider not in DEFAULT_MODELS:
        raise ValueError(f"unknown LLM provider {provider!r}; pick one of {', '.join(PROVIDERS)}")
    model = model or os.environ.get(f"GOA_{provider.upper()}_MODEL") or DEFAULT_MODELS[provider]
    _log.info("LLM provider %s model %s", provider, model)
    if provider == "openai":
        return OpenAICompatible("openai", "https://api.openai.com/v1", "OPENAI_API_KEY", model)
    if provider == "xai":
        return OpenAICompatible("xai", "https://api.x.ai/v1", "XAI_API_KEY", model)
    if provider == "meta":
        return MetaLlama("meta", "https://api.llama.com/compat/v1", "LLAMA_API_KEY", model)
    if provider == "openrouter":
        return OpenAICompatible("openrouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", model,
                                {"X-Title": "Git Ontology Agent"})
    if provider == "anthropic":
        return Anthropic(model)
    return Gemini(model)
