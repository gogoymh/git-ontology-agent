"""Sign-in providers (`claude-code`, `codex`), the connection list and `auto`. No binary, no network:
`goa.oauth.runner` and the status probe are swapped for fakes that check exactly what goa sends."""
import builtins
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock as um

from goa import Agent, cli, connect, mock, oauth
from goa.cli import SCENARIOS
from goa.decision import make_decision
from goa.llm import make_llm
from goa.skills import MAX_STEPS, LLMSkill

KEYS = [v for _, _, v in connect.API_KEYS]


class Isolated(unittest.TestCase):
    """No GOA_* or provider keys from the developer's shell; a scratch config file; fake CLI names."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="goa-oauth-")
        env = {k: v for k, v in os.environ.items() if not k.startswith("GOA_") and k not in KEYS + list(mock.FAKE_KEYS)}
        env.update(GOA_ENV_FILE=os.devnull, GOA_CONFIG=os.path.join(self.tmp, "config", "env"),
                   GOA_CLAUDE_BIN="goa-test-claude", GOA_CODEX_BIN="goa-test-codex")
        p = um.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.calls = []

    def fake_runner(self, reply):
        def run(argv, stdin, timeout, cwd, env):
            self.calls.append({"argv": argv, "stdin": stdin, "cwd": cwd, "env": env})
            return reply(argv) if callable(reply) else reply
        p = um.patch.object(oauth, "runner", run)
        p.start()
        self.addCleanup(p.stop)


class ClaudeCodeProvider(Isolated):
    def test_prints_through_claude_p_with_the_prompt_on_stdin(self):
        self.fake_runner((0, json.dumps({"type": "result", "is_error": False, "result": "Onyx, Salem, Shadow"}), ""))
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-should-not-be-used"
        llm = make_llm("claude-code")
        self.assertEqual((llm.name, llm.model, llm.offline), ("claude-code", "sonnet", False))
        self.assertEqual(llm.complete("You are terse.", "Name a cat"), "Onyx, Salem, Shadow")
        [c] = self.calls
        argv = c["argv"]
        self.assertEqual(argv[0], "goa-test-claude")
        for flag in ("-p", "--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")
        self.assertEqual(argv[argv.index("--system-prompt") + 1], "You are terse.")
        self.assertEqual(argv[argv.index("--tools") + 1], "")          # no tools: an answer, not an agent run
        self.assertNotIn("--json-schema", argv)
        self.assertNotIn("--bare", argv)                                 # --bare would skip the OAuth sign-in
        self.assertEqual(c["stdin"], "Name a cat")
        self.assertNotIn("ANTHROPIC_API_KEY", c["env"])                  # bill the subscription, not a key in .env
        self.assertEqual(os.listdir(c["cwd"]), [])                       # no project CLAUDE.md in the prompt

    def test_structured_output_and_model_override(self):
        self.fake_runner((0, json.dumps({"is_error": False, "result": "{}", "structured_output": {"claims": []}}), ""))
        os.environ["GOA_CLAUDE_CODE_MODEL"] = "haiku"
        self.assertEqual(make_llm("claude-code").model, "haiku")
        llm = make_llm("claude-code:opus")
        schema = {"type": "object", "properties": {"claims": {"type": "array"}}}
        self.assertEqual(llm.json("sys", "p", schema), {"claims": []})
        argv = self.calls[0]["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1]), schema)

    def test_signed_out_says_how_to_sign_in(self):
        self.fake_runner((1, json.dumps({"is_error": True, "result": "Not logged in · Please run /login"}), ""))
        with self.assertRaises(RuntimeError) as e:
            make_llm("claude-code").complete("s", "p")
        self.assertIn("Not logged in", str(e.exception))
        self.assertIn("goa login claude", str(e.exception))

    def test_missing_binary_says_how_to_install(self):
        with self.assertRaises(RuntimeError) as e:
            make_llm("claude-code").complete("s", "p")
        self.assertIn("not installed", str(e.exception))
        self.assertIn("claude.ai/install.sh", str(e.exception))


class CodexProvider(Isolated):
    def test_exec_read_only_with_a_strict_schema_and_the_reply_file(self):
        seen = {}

        def reply(argv):
            with open(argv[argv.index("--output-schema") + 1]) as f:
                seen["schema"] = json.load(f)
            with open(argv[argv.index("-o") + 1], "w") as f:
                f.write('{"claims": []}')
            return 0, '{"type":"turn.completed"}\n', ""
        self.fake_runner(reply)
        os.environ["OPENAI_API_KEY"] = "sk-should-not-be-used"
        llm = make_llm("codex:gpt-5.5")
        schema = {"type": "object", "required": ["claims"], "properties": {"claims": {"type": "array", "items": {
            "type": "object", "required": ["id"], "properties": {"id": {"type": "string"}, "label": {"type": "string"}}}}}}
        self.assertEqual(llm.json("You are the worker.", "Do it", schema), {"claims": []})
        [c] = self.calls
        argv = c["argv"]
        self.assertEqual(argv[:2], ["goa-test-codex", "exec"])
        for flag in ("--skip-git-repo-check", "--ephemeral", "--ignore-rules"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-5.5")
        self.assertIn('model_reasoning_effort="low"', argv)
        self.assertEqual(argv[-1], "-")                                   # the prompt comes on stdin
        self.assertTrue(c["stdin"].startswith("<instructions>\nYou are the worker.\n</instructions>\n\nDo it"))
        self.assertNotIn("OPENAI_API_KEY", c["env"])
        item = seen["schema"]["properties"]["claims"]["items"]
        self.assertEqual((item["additionalProperties"], item["required"]), (False, ["id", "label"]))

    def test_default_model_and_failure(self):
        self.fake_runner((1, "", "error: not logged in"))
        llm = make_llm("codex")
        self.assertEqual(llm.model, "default")
        with self.assertRaises(RuntimeError) as e:
            llm.complete("s", "p")
        self.assertIn("goa login codex", str(e.exception))
        self.assertNotIn("-m", self.calls[0]["argv"])


class NeverReadsTokens(Isolated):
    def test_no_credential_file_is_opened(self):
        home = os.path.expanduser("~")
        guarded = [os.path.join(home, d) for d in (".claude", ".codex", ".config/claude")] + ["/Library/Keychains"]
        real_open = builtins.open

        def guard(path, *a, **k):
            p = os.path.abspath(str(path))
            if any(p.startswith(g) for g in guarded) or p.endswith(("auth.json", ".credentials.json")):
                raise AssertionError(f"goa opened a credential file: {p}")
            return real_open(path, *a, **k)

        def reply(argv):
            if "codex" in argv[0]:
                with real_open(argv[argv.index("-o") + 1], "w") as f:
                    f.write("ok")
                return 0, "", ""
            return 0, json.dumps({"is_error": False, "result": "ok"}), ""
        self.fake_runner(reply)
        probes = {("goa-test-claude", "auth"): (0, json.dumps({"loggedIn": True, "authMethod": "claude.ai",
                                                                "subscriptionType": "pro", "email": "me@x.io"}), ""),
                  ("goa-test-codex", "login"): (0, "", "Logged in using ChatGPT")}
        with um.patch.object(builtins, "open", guard), \
                um.patch.object(oauth.shutil, "which", lambda b: "/usr/bin/" + b), \
                um.patch.object(oauth, "_subprocess", lambda argv, *a: probes[(argv[0], argv[1])]):
            self.assertEqual(make_llm("claude-code").complete("s", "p"), "ok")
            self.assertEqual(make_llm("codex").complete("s", "p"), "ok")
            st = oauth.status()
        self.assertEqual(st["claude-code"], {"installed": True, "ready": True, "detail": "Claude Pro subscription"})
        self.assertEqual(st["codex"], {"installed": True, "ready": True, "detail": "ChatGPT sign-in"})
        self.assertNotIn("me@x.io", json.dumps(st))


class SignInStatus(Isolated):
    def status(self, results, installed=True):
        with um.patch.object(oauth.shutil, "which", lambda b: ("/usr/bin/" + b) if installed else None), \
                um.patch.object(oauth, "_subprocess", lambda argv, *a: results[argv[0]]):
            return oauth.status()

    def test_signed_out_and_not_installed(self):
        out = self.status({"goa-test-claude": (0, json.dumps({"loggedIn": False}), ""),
                           "goa-test-codex": (1, "Not logged in", "")})
        self.assertEqual([out[k]["ready"] for k in ("claude-code", "codex")], [False, False])
        self.assertEqual(out["codex"]["detail"], "signed out")
        out = self.status({}, installed=False)
        self.assertEqual(out["claude-code"], {"installed": False, "ready": False, "detail": "not installed"})

    def test_console_and_api_key_logins(self):
        out = self.status({"goa-test-claude": (0, json.dumps({"loggedIn": True, "authMethod": "console"}), ""),
                           "goa-test-codex": (0, "Logged in using an API key - sk-proj-***abc", "")})
        self.assertEqual(out["claude-code"]["detail"], "Anthropic Console account")
        self.assertEqual(out["codex"]["detail"], "OpenAI API key")
        self.assertNotIn("sk-proj", json.dumps(out))

    def test_login_runs_the_official_flow(self):
        with um.patch.object(oauth.shutil, "which", lambda b: None), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(oauth.login("codex"), 1)
        self.assertIn("npm i -g @openai/codex", out.getvalue())
        with um.patch.object(oauth.shutil, "which", lambda b: "/opt/bin/" + b), \
                um.patch.object(oauth.subprocess, "call", lambda argv: (self.calls.append(argv), 0)[1]):
            self.assertEqual(oauth.login("claude"), 0)
            self.assertEqual(oauth.login("codex"), 0)
        self.assertEqual(self.calls, [["/opt/bin/goa-test-claude", "auth", "login"], ["/opt/bin/goa-test-codex", "login"]])


READY = {"claude-code": {"installed": True, "ready": True, "detail": "Claude Max subscription"},
         "codex": {"installed": True, "ready": True, "detail": "ChatGPT sign-in"}}
NONE = {"claude-code": {"installed": False, "ready": False, "detail": "not installed"},
        "codex": {"installed": True, "ready": False, "detail": "signed out"}}


class Connections(Isolated):
    def test_sign_ins_then_api_keys_then_offline_without_secrets(self):
        os.environ["OPENROUTER_API_KEY"] = "sk-or-v1-secret"
        conns = connect.connections(READY)
        self.assertEqual([c["id"] for c in conns],
                         ["claude-code", "codex", "openrouter", "anthropic", "openai", "gemini", "xai", "meta", "offline"])
        self.assertEqual([c["kind"] for c in conns[:3]], ["sign-in", "sign-in", "api-key"])
        self.assertEqual(conns[0]["detail"], "Claude Max subscription · Claude Code")
        self.assertEqual([c["ready"] for c in conns[2:4]], [True, False])
        self.assertNotIn("sk-or-v1-secret", json.dumps(conns))
        off = connect.connections(NONE)
        self.assertEqual([off[0]["detail"], off[1]["detail"]], ["install Claude Code", "run goa login codex"])

    def test_auto_order(self):
        self.assertEqual(connect.resolve("auto", "auto", connect.connections(READY)), ("claude-code", "llm:claude-code"))
        self.assertEqual(connect.resolve("auto", "auto", connect.connections(
            {**NONE, "codex": READY["codex"]})), ("codex", "llm:codex"))
        os.environ["OPENROUTER_API_KEY"] = "k"
        self.assertEqual(connect.resolve("auto", "auto", connect.connections(NONE)), ("openrouter", "openrouter"))
        del os.environ["OPENROUTER_API_KEY"]
        self.assertEqual(connect.resolve("auto", "auto", connect.connections(NONE)), ("offline", "local"))
        connect.save_default("codex")                                     # a saved choice beats detection
        self.assertEqual((connect.resolve("auto", "auto", connect.connections(READY)), connect.source()),
                         (("codex", "llm:codex"), "saved"))
        os.environ["GOA_LLM"] = "gemini"                                  # the shell or .env beats a saved choice
        os.environ["GOA_DECISION"] = "typesafe"
        self.assertEqual((connect.resolve("auto", "auto", []), connect.source()), (("gemini", "typesafe"), "env"))
        self.assertEqual(connect.resolve("anthropic", "local", []), ("anthropic", "local"))

    def test_save_default_keeps_other_lines(self):
        path = connect.config_path()
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write("GOA_DECISION=local\nGOA_LLM=openai\n")
        connect.save_default("claude-code:opus")
        with open(path) as f:
            self.assertEqual(f.read(), "GOA_DECISION=local\nGOA_LLM=claude-code:opus\n")
        self.assertNotIn("GOA_LLM", os.environ)                           # only `auto` reads it; demo stays offline

    def test_goa_auth_command(self):
        os.environ["OPENROUTER_API_KEY"] = "sk-or-v1-secret"
        with um.patch.object(oauth, "status", lambda: READY):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(["auth"]), 0)
            text = out.getvalue()
            for want in ("Sign in", "API key", "● Claude", "--llm claude-code", "--llm codex", "--llm openrouter",
                         "Default  claude-code (auto) · decision llm:claude-code", "goa login claude"):
                self.assertIn(want, text)
            self.assertNotIn("sk-or-v1-secret", text)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(["auth", "--use", "codex", "--json"]), 0)
            data = json.loads(out.getvalue())
            self.assertEqual(data["default"], {"llm": "codex", "decision": "llm:codex", "source": "saved"})
            self.assertEqual(data["config"], connect.config_path())
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["auth", "--use", "nope"]), 2)


class Factories(Isolated):
    def test_decision_through_a_sign_in(self):
        d = make_decision("llm:claude-code:haiku")
        self.assertEqual((d.name, d.llm.model), ("llm/claude-code", "haiku"))
        self.assertEqual(make_decision("llm:codex").name, "llm/codex")


class SimulatedSignIns(Isolated):
    """--mock answers `claude -p` and `codex exec` too, so the whole online path runs with neither installed."""

    def test_budget_scenario_through_claude_code_and_codex(self):
        d = os.path.join(self.tmp, "conv")
        with mock.session() as sim:
            agent = Agent(d, llm="claude-code", decision="llm:codex", lang="en")
            answers = []
            for msg, ticks in SCENARIOS["budget"]["en"]:
                answers += agent.submit(msg)
                answers += agent.run() if ticks is None else [a for _ in range(ticks) for a in agent.tick()]
        final = answers[-1]
        self.assertEqual(final.kind, "final")
        for want in ("Hotel A (2 nights, ₩180,000)", "within the ₩500,000 budget", "came from your second request"):
            self.assertIn(want, final.text)
        providers = {p for p, _, _ in sim.calls}
        self.assertEqual(providers, {"claude-code", "codex"})
        self.assertIn(("codex", "codex exec", "decision"), sim.calls)

    def test_demo_command(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["demo", "--mock", "--scenario", "cancel", "--llm", "codex", "--decision",
                                       "llm:claude-code", "--lang", "en", "--delay", "0",
                                       "--repo", os.path.join(self.tmp, "r")]), 0)
        text = out.getvalue()
        self.assertIn("llm codex/default (simulated) · decision llm/claude-code (simulated)", text)
        self.assertIn("Stopped the Busan trip plan", text)
        self.assertIn("codex ×", text)


class RealModelsFinish(unittest.TestCase):
    """Real models keep adding "check" claims after they are done; the LLM skill must still finish."""

    def run_with(self, steps):
        class LLM:
            name, offline = "scripted", False

            def __init__(self):
                self.n = 0

            def complete(self, system, prompt, schema=None, max_tokens=1024):
                return "answer"

            def json(self, system, prompt, schema, max_tokens=1024):
                if "New message" in prompt:
                    return {"claims": []}
                self.n += 1
                return steps(self.n)
        d = tempfile.mkdtemp(prefix="goa-finish-")
        self.addCleanup(shutil.rmtree, d, True)
        llm = LLM()
        agent = Agent(d, llm=llm, decision="local", lang="en", skills=[LLMSkill(llm, "en")])
        agent.submit("Name three cats")
        answers = agent.run(max_ticks=50)
        return llm.n, answers, agent

    def claim(self, i, v):
        return {"id": i, "type": "Finding", "value": v, "label": i, "depends_on": []}

    def test_done_with_a_check_claim_ends_next_step(self):
        n, answers, agent = self.run_with(lambda n: {"done": n > 1, "claims": [
            self.claim("names", "Onyx, Salem") if n == 1 else self.claim(f"check.{n}", "all good")], "remaining": []})
        self.assertEqual(n, 2)
        self.assertEqual(answers[-1].kind, "final")
        self.assertEqual(agent.repo.read_state("main").get("names").value, "Onyx, Salem")

    def test_repeating_the_same_claim_counts_as_done(self):
        n, answers, _ = self.run_with(lambda n: {"done": False, "claims": [self.claim("names", "Onyx")], "remaining": ["x"]})
        self.assertEqual((n, answers[-1].kind), (2, "final"))

    def test_never_done_stops_after_max_steps(self):
        n, answers, _ = self.run_with(lambda n: {"done": False, "claims": [self.claim(f"c.{n}", n)], "remaining": ["more"]})
        self.assertEqual((n, answers[-1].kind), (MAX_STEPS, "final"))


if __name__ == "__main__":
    unittest.main()
