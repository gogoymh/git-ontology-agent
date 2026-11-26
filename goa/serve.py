"""`goa serve`: the agent over JSON lines on stdin/stdout, for the React terminal UI and any other front end.

In (one object per line; a line that is not JSON is taken as a message):
    {"type": "submit", "text": "..."}     a user message, any time, even while it works
    {"type": "log"}                        the conversation as a git graph
    {"type": "quit"}                       stop now (end of input instead waits until the work is done)
Out:
    ready     {version, llm, model, decision, lang, repo}
    user      {n, text}                    n is the request number (null for a reply to a question)
    answer    {kind, text, request, relation, relation_name, source, commit}
    step      {branch, subject}            after every work commit
    tasks     {open: [{n, title, branch}]} whenever the open requests change; [] means idle
    submitted {}                           the message is judged and applied
    log       {text}
    error     {message}
Logs go to stderr, so stdout stays pure JSON.
"""
from __future__ import annotations

import contextlib
import json
import sys
import threading
import time
from typing import IO, Any, Dict, Iterator, List, Optional

from . import __version__, log
from .agent import NAMES, Agent, Answer

_log = log.get("serve")
GIVE_UP = 3   # at end of input, stop waiting after this many failed ticks in a row


class Inbox:
    """Lets a user message in between two work steps. The agent's lock alone is not fair: a worker that
    ticks in a loop takes it back at once, so a message sent mid-task would wait for the whole task."""

    def __init__(self) -> None:
        self.cv = threading.Condition()
        self.waiting = 0

    @contextlib.contextmanager
    def message(self) -> Iterator[None]:
        with self.cv:
            self.waiting += 1
        try:
            yield
        finally:
            with self.cv:
                self.waiting -= 1
                self.cv.notify_all()

    def let_in(self) -> None:
        """Called by the worker before each step: wait while a message is being handled."""
        with self.cv:
            while self.waiting:
                self.cv.wait(0.1)


class Events:
    """Thread-safe JSON-lines writer."""

    def __init__(self, out: IO[str]):
        self.out, self.lock = out, threading.Lock()

    def emit(self, type_: str, **data: Any) -> None:
        line = json.dumps({"type": type_, **data}, ensure_ascii=False)
        with self.lock:
            self.out.write(line + "\n")
            self.out.flush()

    def answer(self, a: Answer) -> None:
        self.emit("answer", kind=a.kind, text=a.text, request=a.request, relation=a.relation,
                  relation_name=NAMES.get(a.relation, ""), source=a.source, commit=a.commit)


def ready(ev: Events, agent: Agent) -> None:
    ev.emit("ready", version=__version__, llm=agent.llm.name, model=getattr(agent.llm, "model", ""),
            decision=agent.decision.name, lang=agent.lang, repo=agent.repo.path)


def next_number(agent: Agent, text: str) -> Optional[int]:
    """The request number a message will get: none for an empty line or a reply to a clarifying question."""
    return None if agent.pending or not text.strip() else agent.n + 1


class Server:
    def __init__(self, make_agent, inp: IO[str], out: IO[str]):
        self.ev = Events(out)
        self.inp = inp
        self.agent: Agent = make_agent(self.ev.answer, lambda b, s: self.ev.emit("step", branch=b, subject=s))
        self.stop = threading.Event()
        self.inbox = Inbox()
        self.last: List[Dict[str, Any]] = []
        self.tasks_lock = threading.Lock()
        self.failed = False
        self.errors = 0   # consecutive failed ticks

    def tasks(self) -> None:
        with self.tasks_lock:
            now = [{"n": t.n, "title": t.title, "branch": t.branch} for t in self.agent.open_tasks()]
            if now != self.last:
                self.last = now
                self.ev.emit("tasks", open=now)

    def work(self) -> None:
        while not self.stop.is_set():
            self.inbox.let_in()
            if self.agent.busy():
                try:
                    self.agent.tick()
                    self.errors = 0
                except Exception as e:  # a provider error must not kill the loop
                    _log.error("tick failed: %s", e)
                    self.failed, self.errors = True, self.errors + 1
                    self.ev.emit("error", message=str(e))
                    time.sleep(2)
                self.tasks()
            else:
                time.sleep(0.05)

    def handle(self, line: str) -> bool:
        """One input line; False means quit."""
        line = line.strip()
        if not line:
            return True
        try:
            cmd = json.loads(line) if line.startswith("{") else {"type": "submit", "text": line}
        except ValueError as e:
            self.ev.emit("error", message=f"bad JSON: {e}")
            return True
        kind = cmd.get("type")
        _log.debug("command %s", kind)
        if kind == "quit":
            return False
        if kind == "log":
            self.ev.emit("log", text=self.agent.repo.git("log", "--graph", "--all", "--format=%h %d %s", "-n", "40"))
        elif kind == "submit":
            text = str(cmd.get("text", ""))
            if text.strip():
                with self.inbox.message():
                    self.ev.emit("user", n=next_number(self.agent, text), text=text.strip())
                    try:
                        self.agent.submit(text)
                    except Exception as e:
                        _log.error("submit failed: %s", e)
                        self.failed = True
                        self.ev.emit("error", message=str(e))
                self.ev.emit("submitted")
                self.tasks()
        else:
            self.ev.emit("error", message=f"unknown command type {kind!r}")
        return True

    def run(self) -> int:
        ready(self.ev, self.agent)
        self.tasks()
        worker = threading.Thread(target=self.work, daemon=True)
        worker.start()
        quit_now = False
        try:
            for line in self.inp:
                if not self.handle(line):
                    quit_now = True
                    break
        except KeyboardInterrupt:
            quit_now = True
        if not quit_now:  # end of input: finish the work in progress, like a pipe expects
            while self.agent.busy() and worker.is_alive() and self.errors < GIVE_UP:
                time.sleep(0.05)
        self.stop.set()
        worker.join(timeout=5)
        self.tasks()
        _log.info("serve stopped (%s)", "quit" if quit_now else "end of input")
        return 0


def serve(make_agent, inp: IO[str] = None, out: IO[str] = None) -> int:
    return Server(make_agent, inp or sys.stdin, out or sys.stdout).run()
