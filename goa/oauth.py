"""Sign-in providers: your Claude or ChatGPT subscription, through the official CLIs.

    --llm claude-code   runs `claude -p` (Claude Code), signed in with `claude auth login`
    --llm codex         runs `codex exec` (Codex CLI), signed in with `codex login`

goa never reads, copies or stores an OAuth token. The official CLI signs in, keeps its own
credentials and refreshes them; goa only hands it a prompt on stdin and reads the reply.
Tests and `--mock` swap `runner`, so this path runs with no binary and no network.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import log
from .llm import LLM

_log = log.get("oauth")

TIMEOUT = 300.0
# A sign-in provider must bill the subscription, never an API key that happens to sit in .env.
_DROP = {"claude": ("ANTHROPIC_API_KEY",), "codex": ("OPENAI_API_KEY",)}
INSTALL = {"claude": "curl -fsSL https://claude.ai/install.sh | bash   (or: npm i -g @anthropic-ai/claude-code)",
           "codex": "npm i -g @openai/codex   (or: brew install codex)"}

Run = Callable[[List[str], str, float, str, Dict[str, str]], Tuple[int, str, str]]


def binary(tool: str) -> str:
    """`claude` or `codex`, overridable with GOA_CLAUDE_BIN / GOA_CODEX_BIN."""
    return os.environ.get(f"GOA_{tool.upper()}_BIN") or tool


def _subprocess(argv: List[str], stdin: str, timeout: float, cwd: str, env: Dict[str, str]) -> Tuple[int, str, str]:
    exe = shutil.which(argv[0])
    if not exe:
        tool = "codex" if "codex" in os.path.basename(argv[0]) else "claude"
        raise RuntimeError(f"{argv[0]} is not installed. Install it: {INSTALL[tool]}")
    p = subprocess.run([exe, *argv[1:]], input=stdin, capture_output=True, text=True, timeout=timeout,
                       cwd=cwd, env=env)
    return p.returncode, p.stdout, p.stderr


runner: Run = _subprocess

_quiet_dir: Optional[str] = None
_lock = threading.Lock()


def _cwd() -> str:
    """An empty folder to run in, so no project CLAUDE.md / AGENTS.md leaks into the prompt."""
    global _quiet_dir
    with _lock:
        if not _quiet_dir or not os.path.isdir(_quiet_dir):
            _quiet_dir = tempfile.mkdtemp(prefix="goa-signin-")
    return _quiet_dir


def _env(tool: str) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _DROP[tool]}
    dropped = [k for k in _DROP[tool] if k in os.environ]
    if dropped:
        _log.debug("%s runs without %s so it uses the sign-in", tool, ", ".join(dropped))
    return env


def _tail(text: str, n: int = 300) -> str:
    text = (text or "").strip()
    return text[-n:] if len(text) > n else text


class ClaudeCode(LLM):
    """Claude through Claude Code's print mode. Structured output uses `--json-schema`."""
    name, offline = "claude-code", False

    def __init__(self, model: str = "sonnet"):
        self.model = model

    def command(self, system: str, schema: Optional[Dict[str, Any]] = None) -> List[str]:
        argv = [binary("claude"), "-p", "--output-format", "json", "--model", self.model,
                "--system-prompt", system, "--tools", "", "--no-session-persistence",
                "--setting-sources", "", "--strict-mcp-config", "--disable-slash-commands"]
        if schema:
            argv += ["--json-schema", json.dumps(schema)]
        return argv

    def complete(self, system, prompt, schema=None, max_tokens=1024):
        argv = self.command(system, schema)
        code, out, err = runner(argv, prompt, TIMEOUT, _cwd(), _env("claude"))
        try:
            data = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
        except ValueError:
            data = {}
        if not data or data.get("is_error") or code != 0:
            why = data.get("result") or _tail(err) or _tail(out) or f"exit {code}"
            _log.warning("claude-code failed (exit %s): %s", code, why)
            raise RuntimeError(f"Claude Code: {why}. Sign in with `goa login claude`.")
        _log.debug("claude-code/%s ok turns=%s cost_usd=%s", self.model, data.get("num_turns"),
                   data.get("total_cost_usd"))
        if schema and data.get("structured_output") is not None:
            return json.dumps(data["structured_output"], ensure_ascii=False)
        return str(data.get("result") or "")


def strict(schema: Dict[str, Any]) -> Dict[str, Any]:
    """OpenAI strict schemas: every object closed, every property required."""
    if not isinstance(schema, dict):
        return schema
    out = dict(schema)
    if out.get("type") == "object":
        props = {k: strict(v) for k, v in (out.get("properties") or {}).items()}
        out.update(properties=props, required=list(props), additionalProperties=False)
    if "items" in out:
        out["items"] = strict(out["items"])
    return out


class Codex(LLM):
    """GPT through `codex exec`: read-only sandbox, no session files, reply read from `-o`."""
    name, offline = "codex", False

    def __init__(self, model: str = ""):
        self.model = model or "default"
        self._model = model

    def command(self, out_file: str, schema_file: str = "") -> List[str]:
        argv = [binary("codex"), "exec", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only",
                "--ignore-rules", "--color", "never", "-o", out_file]
        if self._model:
            argv += ["-m", self._model]
        effort = os.environ.get("GOA_CODEX_EFFORT", "low")
        if effort:
            argv += ["-c", f'model_reasoning_effort="{effort}"']
        if schema_file:
            argv += ["--output-schema", schema_file]
        return argv + ["-"]

    @staticmethod
    def stdin(system: str, prompt: str) -> str:
        return f"<instructions>\n{system}\n</instructions>\n\n{prompt}"

    def complete(self, system, prompt, schema=None, max_tokens=1024):
        with tempfile.TemporaryDirectory(prefix="goa-codex-") as d:
            out_file, schema_file = os.path.join(d, "reply.txt"), ""
            if schema:
                schema_file = os.path.join(d, "schema.json")
                with open(schema_file, "w", encoding="utf-8") as f:
                    json.dump(strict(schema), f)
            code, out, err = runner(self.command(out_file, schema_file), self.stdin(system, prompt), TIMEOUT,
                                    _cwd(), _env("codex"))
            text = ""
            if os.path.isfile(out_file):
                with open(out_file, encoding="utf-8") as f:
                    text = f.read().strip()
        if code != 0 or not text:
            why = _tail(err) or _tail(out) or f"exit {code}"
            _log.warning("codex failed (exit %s): %s", code, why)
            raise RuntimeError(f"Codex: {why}. Sign in with `goa login codex`.")
        _log.debug("codex/%s ok chars=%d", self.model, len(text))
        return text


# --- sign-in status ---------------------------------------------------------------
def _probe(argv: List[str]) -> Tuple[int, str]:
    try:
        code, out, err = _subprocess(argv, "", 20.0, _cwd(), dict(os.environ))
        return code, (out or "") + (err or "")
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        return 127, str(e)


def claude_status() -> Dict[str, Any]:
    """{installed, ready, detail}; never returns the account e-mail or org."""
    if not shutil.which(binary("claude")):
        return {"installed": False, "ready": False, "detail": "not installed"}
    code, text = _probe([binary("claude"), "auth", "status", "--json"])
    try:
        data = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except ValueError:
        data = {}
    if not data.get("loggedIn"):
        return {"installed": True, "ready": False, "detail": "signed out"}
    plan = data.get("subscriptionType") or ""
    if data.get("authMethod") == "claude.ai":
        detail = f"Claude {plan.capitalize()} subscription".replace("  ", " ") if plan else "Claude subscription"
    else:
        detail = "Anthropic Console account"
    return {"installed": True, "ready": True, "detail": detail}


def codex_status() -> Dict[str, Any]:
    if not shutil.which(binary("codex")):
        return {"installed": False, "ready": False, "detail": "not installed"}
    code, text = _probe([binary("codex"), "login", "status"])
    low = text.lower()
    if code != 0 or "not logged in" in low or "logged in" not in low:
        return {"installed": True, "ready": False, "detail": "signed out"}
    return {"installed": True, "ready": True, "detail": "ChatGPT sign-in" if "chatgpt" in low else "OpenAI API key"}


def status() -> Dict[str, Dict[str, Any]]:
    """Both probes at once (each starts a CLI, which takes about a second)."""
    out: Dict[str, Dict[str, Any]] = {}
    threads = [threading.Thread(target=lambda k=k, f=f: out.__setitem__(k, f()))
               for k, f in (("claude-code", claude_status), ("codex", codex_status))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    _log.info("sign-in status %s", {k: (v["ready"], v["detail"]) for k, v in out.items()})
    return out


def login(tool: str) -> int:
    """Hand the terminal to the official sign-in flow (browser OAuth)."""
    argv = {"claude": [binary("claude"), "auth", "login"], "codex": [binary("codex"), "login"]}[tool]
    exe = shutil.which(argv[0])
    if not exe:
        print(f"{argv[0]} is not installed. Install it first:\n  {INSTALL[tool]}")
        return 1
    _log.info("starting %s sign-in", tool)
    return subprocess.call([exe, *argv[1:]])
