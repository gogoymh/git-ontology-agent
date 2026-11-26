"""A real git repository driven with plumbing only: no checkout, no working tree races.

Every request is a branch `req/<n>`, every claim change is a commit with
trailers, every answer is a commit on `answers`, and merges are true
two-parent merge commits on `main`.
"""
from __future__ import annotations

import os
import subprocess
from typing import Dict, List, Optional, Tuple

from . import log
from .ontology import STATE_FILE, State

_log = log.get("git")

_ENV = {
    "GIT_AUTHOR_NAME": "Git Ontology Agent", "GIT_AUTHOR_EMAIL": "goa@localhost",
    "GIT_COMMITTER_NAME": "Git Ontology Agent", "GIT_COMMITTER_EMAIL": "goa@localhost",
}
ANSWERS = "answers"


class Repo:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        os.makedirs(self.path, exist_ok=True)
        if not os.path.isdir(os.path.join(self.path, ".git")) and not os.path.exists(os.path.join(self.path, "HEAD")):
            self.git("init", "-q", "-b", "main")
            _log.info("initialized repo at %s", self.path)
        self.policy = ""

    # --- low level ----------------------------------------------------------
    def git(self, *args: str, input: Optional[str] = None) -> str:
        env = {**os.environ, **_ENV}
        res = subprocess.run(["git", "-C", self.path, *args], input=input, capture_output=True,
                             text=True, env=env)
        if res.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {res.stderr.strip()}")
        return res.stdout.strip()

    def rev(self, ref: str) -> Optional[str]:
        try:
            return self.git("rev-parse", "-q", "--verify", f"refs/heads/{ref}^{{commit}}")
        except RuntimeError:
            return None

    def short(self, sha: str) -> str:
        return sha[:7]

    def _tree(self, files: Dict[str, str]) -> str:
        entries = []
        for name, content in sorted(files.items()):
            blob = self.git("hash-object", "-w", "--stdin", input=content)
            entries.append(f"100644 blob {blob}\t{name}")
        return self.git("mktree", input="\n".join(entries) + ("\n" if entries else ""))

    def _commit(self, tree: str, parents: List[str], message: str) -> str:
        args = ["commit-tree", tree]
        for p in parents:
            args += ["-p", p]
        return self.git(*args, "-F", "-", input=message)

    def _set(self, ref: str, sha: str, old: Optional[str]) -> None:
        self.git("update-ref", f"refs/heads/{ref}", sha, *( [old] if old else [] ))

    # --- state ----------------------------------------------------------------
    def read_state(self, ref: str) -> State:
        try:
            return State.from_yaml(self.git("show", f"{ref}:{STATE_FILE}"))
        except RuntimeError:
            return State()

    def commit_state(self, branch: str, state: State, subject: str, trailers: Dict[str, str],
                     extra_parent: Optional[str] = None) -> str:
        head = self.rev(branch)
        files = {STATE_FILE: state.to_yaml()}
        if self.policy:
            files["policy.yaml"] = self.policy
        tree = self._tree(files)
        parents = [p for p in (head, extra_parent) if p]
        sha = self._commit(tree, parents, _message(subject, trailers))
        self._set(branch, sha, head)
        _log.info("commit %s on %s: %s", self.short(sha), branch, subject)
        return sha

    def ensure_main(self, policy: str = "") -> str:
        self.policy = policy
        head = self.rev("main")
        if head:
            return head
        sha = self.commit_state("main", State(), "C0: empty conversation state", {"Answer": "none"})
        self.git("symbolic-ref", "HEAD", "refs/heads/main")
        return sha

    def branch(self, name: str, from_ref: str = "main") -> str:
        sha = self.rev(from_ref)
        if sha is None:
            raise RuntimeError(f"no such ref {from_ref}")
        self._set(name, sha, None)
        _log.info("branch %s from %s@%s", name, from_ref, self.short(sha))
        return sha

    def close(self, name: str, to: str) -> None:
        """Move a branch out of the way without losing history (e.g. req/1 -> cancelled/req/1)."""
        sha = self.rev(name)
        if sha:
            self._set(to, sha, None)
            self.git("update-ref", "-d", f"refs/heads/{name}")
            _log.info("closed %s as %s", name, to)

    def _ref(self, name: str) -> str:
        return f"refs/heads/{name}" if self.rev(name) is not None else name

    def is_ancestor(self, a: str, b: str) -> bool:
        res = subprocess.run(
            ["git", "-C", self.path, "merge-base", "--is-ancestor", self._ref(a), self._ref(b)],
            capture_output=True, text=True, env={**os.environ, **_ENV},
        )
        if res.returncode in (0, 1):
            return res.returncode == 0
        raise RuntimeError(f"git merge-base --is-ancestor {a} {b} failed: {res.stderr.strip()}")

    def merge_base(self, a: str, b: str) -> str:
        return self.git("merge-base", self._ref(a), self._ref(b))

    def merges(self, ref: str = "main", n: int = 200) -> List[Tuple[str, str, str]]:
        """Recent two-parent merge commits on `ref` as (merge_sha, first_parent, second_parent)."""
        out = self.git("rev-list", "--merges", "--parents", f"-n{n}", self._ref(ref))
        rows: List[Tuple[str, str, str]] = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 3:
                rows.append((parts[0], parts[1], parts[2]))
        return rows

    def merge_commit(self, branch: str, state: State, subject: str, trailers: Dict[str, str]) -> str:
        return self.commit_state("main", state, subject, trailers, extra_parent=self.rev(branch))

    def record_answer(self, kind: str, text: str, source: str, extra: Dict[str, str]) -> str:
        """Answers live on their own branch so they never disturb state, yet stay replayable."""
        head = self.rev(ANSWERS)
        tree = self._tree({"answer.txt": text + "\n"})
        first = text.splitlines()[0] if text else ""
        sha = self._commit(tree, [head] if head else [],
                           _message(f"answer({kind}): {first[:60]}", {"Answer": kind, "Source": source, **extra}))
        self._set(ANSWERS, sha, head)
        return sha

    def branches(self, prefix: str = "") -> List[str]:
        out = self.git("for-each-ref", "--format=%(refname:short)", f"refs/heads/{prefix}")
        return [l for l in out.splitlines() if l]

    def log(self, ref: str = "main", n: int = 50) -> List[Tuple[str, str]]:
        out = self.git("log", f"-{n}", "--format=%H%x09%B%x1e", ref)
        rows = []
        for chunk in out.split("\x1e"):
            chunk = chunk.strip()
            if chunk:
                sha, _, body = chunk.partition("\t")
                rows.append((sha, body))
        return rows


def _message(subject: str, trailers: Dict[str, str]) -> str:
    body = "\n".join(f"{k}: {v}" for k, v in trailers.items() if v not in (None, ""))
    return f"{subject}\n\n{body}\n" if body else subject + "\n"
