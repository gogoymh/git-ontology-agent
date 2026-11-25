"""What this machine can talk to right now, and `auto`: the best of it, without asking.

Two ways in, side by side:
    sign-in   your Claude or ChatGPT subscription through Claude Code / Codex (goa.oauth)
    API key   any key in the environment or ./.env (goa.llm)

`--llm auto` takes GOA_LLM when it is set (shell or ./.env, then the default saved by `goa auth --use`),
then the first ready sign-in, then the first API key, then offline.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from . import env, log, oauth

_log = log.get("connect")

# (spec, label, key variable), in the order `auto` tries them
API_KEYS = [("openrouter", "OpenRouter", "OPENROUTER_API_KEY"), ("anthropic", "Anthropic", "ANTHROPIC_API_KEY"),
            ("openai", "OpenAI", "OPENAI_API_KEY"), ("gemini", "Gemini", "GEMINI_API_KEY"),
            ("xai", "xAI", "XAI_API_KEY"), ("meta", "Meta Llama", "LLAMA_API_KEY")]
SIGN_INS = [("claude-code", "Claude", "claude", "Claude Code"), ("codex", "Codex", "codex", "Codex CLI")]


def config_path() -> str:
    """Where `goa auth --use` saves the default: GOA_CONFIG, else $XDG_CONFIG_HOME/goa/env."""
    if os.environ.get("GOA_CONFIG"):
        return os.environ["GOA_CONFIG"]
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "goa", "env")


def connections(signins: Optional[Dict[str, Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Every way to connect, sign-ins first. Never includes a key, an e-mail or an org name."""
    st = oauth.status() if signins is None else signins
    out: List[Dict[str, Any]] = []
    for spec, label, tool, via in SIGN_INS:
        s = st.get(spec) or {"installed": False, "ready": False, "detail": "not installed"}
        if s["ready"]:
            detail = f"{s['detail']} · {via}"
        elif s["installed"]:
            detail = f"run goa login {tool}"
        else:
            detail = f"install {via}"
        out.append({"id": spec, "kind": "sign-in", "label": label, "ready": bool(s["ready"]), "detail": detail})
    for spec, label, var in API_KEYS:
        ready = bool(os.environ.get(var))
        out.append({"id": spec, "kind": "api-key", "label": label, "ready": ready,
                    "detail": var if ready else f"set {var}"})
    out.append({"id": "offline", "kind": "offline", "label": "Offline", "ready": True,
                "detail": "built-in demo skills, no network"})
    return out


def _real(v: str) -> str:
    v = (v or "").strip()
    return "" if v.lower() == "auto" else v


def _configured(var: str) -> str:
    """The shell or ./.env first, then the default saved by `goa auth --use` (read only by `auto`)."""
    return _real(os.environ.get(var, "")) or _real(env.parse(config_path()).get(var, ""))


def source(var: str = "GOA_LLM") -> str:
    """'env' (shell or ./.env), 'saved' (goa auth --use) or 'auto' (nothing set)."""
    if _real(os.environ.get(var, "")):
        return "env"
    return "saved" if _real(env.parse(config_path()).get(var, "")) else "auto"


def resolve(llm: str = "auto", decision: str = "auto",
            conns: Optional[List[Dict[str, Any]]] = None) -> Tuple[str, str]:
    """Turn `auto` into real specs. Only probes the sign-ins when it has to."""
    if (llm or "auto") == "auto":
        llm = _configured("GOA_LLM")
        if not llm:
            conns = conns if conns is not None else connections()
            llm = next(c["id"] for c in conns if c["ready"])
            _log.info("auto picked llm %s", llm)
    if (decision or "auto") == "auto":
        decision = _configured("GOA_DECISION") or _decision_for(llm)
    return llm, decision


def _decision_for(llm: str) -> str:
    provider = llm.split(":", 1)[0]
    if provider == "offline":
        return "local"
    if provider == "openrouter":
        return "openrouter"          # Jev through OpenRouter's Decisions API, same key
    return f"llm:{llm}"             # judge with the same model the user connected


def save_default(llm: str) -> str:
    """Write GOA_LLM to the config file, keeping its other lines; returns the path."""
    path = config_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    lines: List[str] = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            lines = [l.rstrip("\n") for l in f if not re.match(r"\s*(export\s+)?GOA_LLM\s*=", l)]
    lines.append(f"GOA_LLM={llm}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    _log.info("saved default llm %s to %s", llm, path)
    return path
