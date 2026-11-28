"""Command line: `goa` (the React terminal UI), `goa ask`, `goa chat`, `goa serve`, `goa mcp`, `goa auth`, `goa login`,
`goa demo`, `goa log`."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import List, Optional

from . import __version__, connect, env, i18n, log, mock, oauth
from .agent import NAMES, Agent, Answer
from .decision import PROVIDERS as DECISIONS
from .llm import PROVIDERS as LLMS
from .llm import make_llm
from .serve import Events, Inbox, next_number
from .serve import ready as emit_ready
from .serve import HISTORY_LIMIT, serve

_log = log.get("cli")

SCENARIOS = {
    # (message, ticks to run after it); None ticks = run until idle
    "budget": {
        "en": [("Plan my Busan business trip next week", 2), ("Oh, keep the budget under 500,000 won", 0),
               ("By the way, what time is tomorrow's meeting?", None)],
        "ko": [("다음 주 부산 출장 일정 짜줘", 2), ("아 예산은 50만원 이하로", 0), ("근데 내일 회의 몇 시였지?", None)],
    },
    "correction": {
        "en": [("Plan my Busan business trip next week", 2), ("Actually, not Busan but Daegu", None)],
        "ko": [("다음 주 부산 출장 일정 짜줘", 2), ("아 부산 말고 대구야", None)],
    },
    "cancel": {
        "en": [("Plan my Busan business trip next week", 2), ("How far along are you?", 0),
               ("The trip got cancelled, you can stop", None)],
        "ko": [("다음 주 부산 출장 일정 짜줘", 2), ("어디까지 됐어?", 0), ("아 출장 취소됐대. 그만해도 돼", None)],
    },
}

_TTY = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _TTY else s


class Live:
    """One transient status line under the conversation (terminal only), so long LLM calls never look frozen."""

    def __init__(self):
        self.on = False

    def show(self, text: str) -> None:
        if _TTY:
            sys.stdout.write("\r\033[K" + _c("2", f"  ⋯ {text[:100]}"))
            sys.stdout.flush()
            self.on = True

    def clear(self) -> None:
        if self.on:
            sys.stdout.write("\r\033[K")
            self.on = False


_live = Live()


_LABEL = 15                       # width of "Agent (interim)"; text starts at column 2 + _LABEL + 2
_TEXT = " " * (2 + _LABEL + 2)


def _print_user(n: int, msg: str) -> None:
    print("\n" + _c("1;36", f"{f'User (R{n})':<{2 + _LABEL}}") + f"  {msg}", flush=True)


def _print_answers(answers: List[Answer]) -> None:
    _live.clear()
    for a in answers:
        label = f"{f'Agent ({a.kind})':<{_LABEL}}"
        tag = _c("1;32", label) if a.kind == "final" else _c("1;33", label)
        rel = f" {a.relation} {NAMES[a.relation]}" if a.relation else ""
        print(f"  {tag}  {a.text}")
        print(_c("2", f"{_TEXT}↳ from {a.source}{rel}"), flush=True)


def _streaming_agent(args, repo: str, lang: str, pause: float = 0.0) -> Agent:
    """Answers print the moment they exist; every work commit updates the live line.
    pause > 0 (demo only): show 'Agent is typing…' for that long before each answer."""
    def on_answer(a: Answer) -> None:
        if pause:
            _live.show("Agent is typing…")
            time.sleep(pause)
        _print_answers([a])
    return Agent(repo, llm=args.llm, decision=args.decision, lang=lang,
                 on_answer=on_answer, on_step=lambda b, s: _live.show(f"{b} · {s}"))


def _delay(value: str) -> float:
    """argparse type for --delay / GOA_DEMO_DELAY: seconds, >= 0."""
    try:
        v = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}")
    if v < 0 or v != v:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {value}")
    return v


def _mock(args):
    """--mock: simulated provider APIs; defaults to Anthropic + Typesafe so the online path runs."""
    if not args.mock:
        return contextlib.nullcontext(None)
    if args.llm == "offline":
        args.llm = "anthropic"
    if args.decision == "local":
        args.decision = "typesafe"
    return mock.session()


def cmd_demo(args) -> int:
    _resolve(args)
    lang = i18n.template_lang(i18n.detect(args.lang))
    repo = args.repo or tempfile.mkdtemp(prefix="goa-demo-")
    delay = args.delay if args.delay is not None else (0.8 if sys.stdout.isatty() else 0.0)
    _log.debug("demo delay %.2fs between turns, %.2fs before answers (%s)", delay, delay / 2,
               "--delay/GOA_DEMO_DELAY" if args.delay is not None else "default for tty" if delay else "default, not a tty")
    with _mock(args) as sim:
        agent = _streaming_agent(args, repo, lang, pause=delay / 2)
        sim_tag = " (simulated)" if sim else ""
        print(_c("1", f"Git Ontology Agent · scenario '{args.scenario}' · repo {repo}"))
        if sim:
            print(_c("2", f"llm {agent.llm.name}/{getattr(agent.llm, 'model', '')}{sim_tag} · "
                          f"decision {agent.decision.name}{sim_tag} · no key, no network"))
        for i, (msg, ticks) in enumerate(SCENARIOS[args.scenario][lang], 1):
            seen = len(sim.calls) if sim else 0
            if delay:
                time.sleep(delay)
            _print_user(i, msg)
            _live.show("reading your message…")
            agent.submit(msg)
            if ticks is None:
                agent.run()
            for _ in range(ticks or 0):
                agent.tick()
            _live.clear()
            if sim:
                print(_c("2", f"  {'api':<{_LABEL}}  {mock.summary(sim.calls[seen:])}"))
        print("\n" + _c("2", agent.repo.git("log", "--graph", "--all", "--format=%h %d %s", "-n", "40")))
        if sim:
            print(_c("2", f"\n{len(sim.calls)} simulated API calls ({mock.summary(sim.calls)}), 0 network requests"))
    return 0


def _resolve(args) -> None:
    """`auto` -> the specs it stands for (GOA_LLM, a ready sign-in, an API key, or offline)."""
    args.llm, args.decision = connect.resolve(args.llm, args.decision)
    _log.info("%s with llm %s, decision %s", args.cmd or "goa", args.llm, args.decision)


def cmd_chat(args) -> int:
    _resolve(args)
    with _mock(args):
        return _chat(args)


def _chat(args) -> int:
    agent = _streaming_agent(args, args.repo, args.lang)
    stop, inbox = threading.Event(), Inbox()

    def worker():
        while not stop.is_set():
            inbox.let_in()
            if agent.busy():
                try:
                    agent.tick()
                except Exception as e:  # a provider error must not kill the loop
                    _log.error("tick failed: %s", e)
                    print(_c("31", f"  error: {e}"))
                    time.sleep(2)
            else:
                time.sleep(0.1)

    threading.Thread(target=worker, daemon=True).start()
    print(_c("1", f"Git Ontology Agent {__version__} · llm {agent.llm.name} · decision {agent.decision.name}"))
    print(_c("2", "Talk any time, even while it works. Ctrl-D to quit."))
    try:
        for line in sys.stdin:
            with inbox.message():
                agent.submit(line)
    except KeyboardInterrupt:
        pass
    stop.set()
    return 0


def cmd_ask(args) -> int:
    """One shot: send the message(s), run until every request is merged, print the answers."""
    msgs = [m for m in args.message if m.strip()]
    if not msgs and not sys.stdin.isatty():
        msgs = [l for l in sys.stdin.read().splitlines() if l.strip()]
    if not msgs:
        print("goa ask: give a message, e.g. goa ask \"Plan my Busan business trip next week\"", file=sys.stderr)
        return 2
    _resolve(args)
    with _mock(args):
        ev = Events(sys.stdout) if args.json else None
        agent = Agent(args.repo, llm=args.llm, decision=args.decision, lang=args.lang,
                      on_answer=ev.answer if ev else lambda a: _print_answers([a]),
                      on_step=(lambda b, s: ev.emit("step", branch=b, subject=s)) if ev
                      else lambda b, s: _live.show(f"{b} · {s}"))
        if ev:
            emit_ready(ev, agent)
        try:
            for m in msgs:
                n = next_number(agent, m)
                if ev:
                    ev.emit("user", n=n, text=m.strip())
                elif len(msgs) > 1:
                    _print_user(n or 0, m.strip())
                agent.submit(m)
            agent.run()
        except Exception as e:
            _live.clear()
            _log.error("ask failed: %s", e)
            if ev:
                ev.emit("error", message=str(e))
            else:
                print(_c("31", f"error: {e}"), file=sys.stderr)
            return 1
        _live.clear()
    return 0


def cmd_serve(args) -> int:
    _resolve(args)
    with _mock(args):
        return serve(lambda on_answer, on_step: Agent(args.repo, llm=args.llm, decision=args.decision,
                                                      lang=args.lang, on_answer=on_answer, on_step=on_step),
                     history_limit=args.history)


def cmd_mcp(args) -> int:
    from .mcp import serve_mcp

    _resolve(args)
    repo_dir = contextlib.nullcontext(args.repo) if args.repo else tempfile.TemporaryDirectory(prefix="goa-mcp-")
    with repo_dir as repo, _mock(args):
        return serve_mcp(lambda: Agent(repo, llm=args.llm, decision=args.decision, lang=args.lang),
                         sys.stdin.buffer, sys.stdout.buffer)


def cmd_auth(args) -> int:
    """Every way to connect (sign-ins and API keys), and the default `auto` would pick."""
    if args.use:
        try:
            make_llm(args.use)
        except ValueError as e:
            print(f"goa auth: {e}", file=sys.stderr)
            return 2
        connect.save_default(args.use)
    conns = connect.connections()
    llm, decision = connect.resolve("auto", "auto", conns)
    default = {"llm": llm, "decision": decision, "source": connect.source()}
    if args.json:
        print(json.dumps({"connections": conns, "default": default, "config": connect.config_path()},
                         ensure_ascii=False))
        return 0
    if args.use:
        print(_c("1;32", "✓") + f" default saved: {args.use}  " + _c("2", connect.config_path()))
    print(_c("1", "Connections"))
    for kind, title in (("sign-in", "Sign in"), ("api-key", "API key"), ("offline", "Offline")):
        print(_c("2", f"  {title}"))
        for c in (c for c in conns if c["kind"] == kind):
            dot = _c("32", "●") if c["ready"] else _c("2", "○")
            line = f"    {dot} {c['label']:<11} {c['detail']:<44}"
            print(line + _c("2", f"--llm {c['id']}") if c["ready"] else _c("2", line.rstrip()))
    how = {"env": "GOA_LLM", "saved": "saved", "auto": "auto"}[default["source"]]
    print(f"\nDefault  {_c('1', llm)} ({how}) · decision {decision}")
    print(_c("2", "Sign in: goa login claude · goa login codex   Save a default: goa auth --use <provider>"))
    return 0


def cmd_login(args) -> int:
    code = oauth.login(args.tool)
    if code == 0:
        spec = {"claude": "claude-code", "codex": "codex"}[args.tool]
        print(_c("1;32", "✓") + f" signed in. goa uses it when nothing else is set, or: goa --llm {spec}")
    return code


UI_DIR = Path(__file__).resolve().parent.parent / "ui"
NODE_MAJOR = 22   # Ink 8


def _ui_entry() -> Optional[str]:
    p = os.environ.get("GOA_UI") or str(UI_DIR / "dist" / "cli.js")
    return p if os.path.isfile(p) else None


def _node_major(node: str) -> int:
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10).stdout
        return int(out.strip().lstrip("v").split(".")[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def cmd_ui(args, bare: bool = False) -> int:
    """The React (Ink) terminal UI. It runs `goa serve` with this Python and draws what it says.
    Bare `goa` falls back to `goa chat` when the UI cannot start."""
    node, entry, why = shutil.which("node"), _ui_entry(), ""
    if not node:
        why = f"Node.js {NODE_MAJOR}+ is not installed"
    elif _node_major(node) < NODE_MAJOR:
        why = f"Node.js {NODE_MAJOR}+ is needed (found {_node_major(node) or 'unknown'})"
    elif not entry and (UI_DIR / "package.json").is_file() and shutil.which("npm"):
        print(_c("2", "Building the terminal UI once: npm install in ui/ …"), flush=True)
        _log.info("building UI in %s", UI_DIR)
        subprocess.call([shutil.which("npm"), "--prefix", str(UI_DIR), "install", "--no-audit", "--no-fund"])
        entry = _ui_entry()
        why = "" if entry else "the UI build failed (see npm output above)"
    elif not entry:
        why = "the terminal UI is not built: npm --prefix ui install" if (UI_DIR / "package.json").is_file() \
            else "the terminal UI ships in the repository's ui/ folder"
    if why:
        _log.info("UI unavailable: %s", why)
        if not bare:
            print(f"goa ui: {why}", file=sys.stderr)
            return 1
        print(_c("2", f"({why}; using the plain chat)"))
        return cmd_chat(args)
    argv = [node, entry]
    for flag in ("llm", "decision"):
        if getattr(args, flag) not in ("", "auto"):
            argv += [f"--{flag}", getattr(args, flag)]
    for flag in ("lang", "repo"):
        if getattr(args, flag):
            argv += [f"--{flag}", getattr(args, flag)]
    if args.mock:
        argv.append("--mock")
    root = str(Path(__file__).resolve().parent.parent)
    child_env = {**os.environ, "GOA_PYTHON": sys.executable,
                 "PYTHONPATH": os.pathsep.join(p for p in (root, os.environ.get("PYTHONPATH", "")) if p)}
    _log.info("starting UI %s", " ".join(argv[1:]))
    proc = subprocess.Popen(argv, env=child_env)
    while True:
        try:
            return proc.wait()
        except KeyboardInterrupt:   # the UI handles Ctrl-C itself; wait for it to close
            continue


def cmd_log(args) -> int:
    from .gitstore import Repo
    print(Repo(args.repo).git("log", "--graph", "--all", "--format=%h %d %s%n%b", "-n", str(args.n)))
    return 0


def main(argv=None) -> int:
    env.load(os.environ.get("GOA_ENV_FILE", ".env"))
    p = argparse.ArgumentParser(prog="goa", description="Git Ontology Agent: talk to an agent mid-task; "
                                "git branches and an ontology merge every request into one answer. "
                                "Run `goa` alone for the terminal UI.")
    p.add_argument("--version", action="version", version=f"goa {__version__}")
    sub = p.add_subparsers(dest="cmd")

    def common(sp, repo_default=None, llm_default="auto", decision_default="auto"):
        sp.add_argument("--llm", default=llm_default,
                        help=f"provider[:model] or auto, one of {', '.join(LLMS)} (default {llm_default})")
        sp.add_argument("--decision", default=decision_default,
                        help=f"provider[:model] or auto, one of {', '.join(DECISIONS)} (default {decision_default})")
        sp.add_argument("--lang", default="", help="answer language (default: system locale)")
        sp.add_argument("--repo", default=repo_default, help="git repo that stores the conversation")
        sp.add_argument("--mock", action="store_true",
                        help="simulate the provider APIs (no key, no network); defaults to anthropic + typesafe")

    u = sub.add_parser("ui", help="the React terminal UI (what bare `goa` opens)")
    common(u, ".goa")
    u.set_defaults(fn=cmd_ui)
    a = sub.add_parser("ask", help="one shot: send messages, print the answers when the work is merged")
    a.add_argument("message", nargs="*", help="message(s); read from stdin when none are given")
    a.add_argument("--json", action="store_true", help="print JSON lines (same events as goa serve)")
    common(a, ".goa")
    a.set_defaults(fn=cmd_ask)
    c = sub.add_parser("chat", help="interactive session, plain text")
    common(c, ".goa")
    c.set_defaults(fn=cmd_chat)
    sv = sub.add_parser("serve", help="JSON lines over stdin/stdout, for the UI and other front ends")
    common(sv, ".goa")
    sv.add_argument("--history", type=int, default=HISTORY_LIMIT, metavar="N",
                    help=f"answers replayed at start (default {HISTORY_LIMIT}; 0 = none)")
    sv.set_defaults(fn=cmd_serve)
    m = sub.add_parser("mcp", help="serve GOA tools to local MCP clients over stdio")
    common(m)
    m.set_defaults(fn=cmd_mcp)
    au = sub.add_parser("auth", help="list sign-ins and API keys; --use saves the default")
    au.add_argument("--use", metavar="PROVIDER", help="save the default --llm (e.g. claude-code, codex, openrouter)")
    au.add_argument("--json", action="store_true", help="machine-readable")
    au.set_defaults(fn=cmd_auth)
    li = sub.add_parser("login", help="sign in with Claude Code or Codex (opens the browser)")
    li.add_argument("tool", choices=("claude", "codex"))
    li.set_defaults(fn=cmd_login)
    d = sub.add_parser("demo", help="replay a spec scenario (offline by default)")
    d.add_argument("--scenario", choices=sorted(SCENARIOS), default="budget")
    d.add_argument("--delay", type=_delay, default=os.environ.get("GOA_DEMO_DELAY") or None, metavar="SECONDS",
                   help="pause between turns, half of it before each answer "
                        "(env GOA_DEMO_DELAY; default 0.8 on a terminal, 0 otherwise)")
    common(d, llm_default=os.environ.get("GOA_LLM", "offline"), decision_default=os.environ.get("GOA_DECISION", "local"))
    d.set_defaults(fn=cmd_demo)
    lg = sub.add_parser("log", help="show the conversation as a git graph")
    lg.add_argument("--repo", default=".goa")
    lg.add_argument("-n", type=int, default=60)
    lg.set_defaults(fn=cmd_log)
    args = p.parse_args(argv)
    if args.cmd is None:   # bare `goa`: the terminal UI on a terminal, help otherwise
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            p.print_help()
            return 2
        return cmd_ui(argparse.Namespace(cmd=None, llm="auto", decision="auto", lang="", repo=".goa", mock=False),
                      bare=True)
    return args.fn(args)

if __name__ == "__main__":
    raise SystemExit(main())
