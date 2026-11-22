"""Command line: `goa demo`, `goa chat`, `goa log`."""
from __future__ import annotations

import argparse
import contextlib
import os
import sys
import tempfile
import threading
import time
from typing import List

from . import __version__, env, i18n, log, mock
from .agent import NAMES, Agent, Answer
from .decision import PROVIDERS as DECISIONS
from .llm import PROVIDERS as LLMS

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


def cmd_chat(args) -> int:
    with _mock(args):
        return _chat(args)


def _chat(args) -> int:
    agent = _streaming_agent(args, args.repo, args.lang)
    stop = threading.Event()

    def worker():
        while not stop.is_set():
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
            agent.submit(line)
    except KeyboardInterrupt:
        pass
    stop.set()
    return 0


def cmd_log(args) -> int:
    from .gitstore import Repo
    print(Repo(args.repo).git("log", "--graph", "--all", "--format=%h %d %s%n%b", "-n", str(args.n)))
    return 0


def main(argv=None) -> int:
    env.load(os.environ.get("GOA_ENV_FILE", ".env"))
    p = argparse.ArgumentParser(prog="goa", description="Git Ontology Agent: talk to an agent mid-task; "
                                "git branches and an ontology merge every request into one answer.")
    p.add_argument("--version", action="version", version=f"goa {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, repo_default=None):
        sp.add_argument("--llm", default=os.environ.get("GOA_LLM", "offline"),
                        help=f"provider[:model], one of {', '.join(LLMS)}")
        sp.add_argument("--decision", default=os.environ.get("GOA_DECISION", "local"),
                        help=f"provider[:model], one of {', '.join(DECISIONS)}")
        sp.add_argument("--lang", default="", help="answer language (default: system locale)")
        sp.add_argument("--repo", default=repo_default, help="git repo that stores the conversation")
        sp.add_argument("--mock", action="store_true",
                        help="simulate the provider APIs (no key, no network); defaults to anthropic + typesafe")

    d = sub.add_parser("demo", help="replay a spec scenario (offline by default)")
    d.add_argument("--scenario", choices=sorted(SCENARIOS), default="budget")
    d.add_argument("--delay", type=_delay, default=os.environ.get("GOA_DEMO_DELAY") or None, metavar="SECONDS",
                   help="pause between turns, half of it before each answer "
                        "(env GOA_DEMO_DELAY; default 0.8 on a terminal, 0 otherwise)")
    common(d)
    d.set_defaults(fn=cmd_demo)
    c = sub.add_parser("chat", help="interactive session")
    common(c, ".goa")
    c.set_defaults(fn=cmd_chat)
    lg = sub.add_parser("log", help="show the conversation as a git graph")
    lg.add_argument("--repo", default=".goa")
    lg.add_argument("-n", type=int, default=60)
    lg.set_defaults(fn=cmd_log)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
