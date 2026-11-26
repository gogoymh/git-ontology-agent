"""The engine: requests become branches, follow-ups are judged by a decision model,
interim answers read the branch, the final answer reads the merge commit.

    1 open request?   (git)       no -> branch req/<n> from main, go to 4
    2 relation        (decision)  supplement k1 / correction k2 / independent k3 / cancel k4 / progress k5
    3 interim answer  (LLM)       from the branch head as it is right now
    4 process         (LLM+ontology) one claim commit per step; only dependents of a change are redone
    5 merge           (git+decision) claim-level 3-way merge, type rules, noul for the rest
    6 final answer    (LLM)       from the merge commit, base..M, with provenance
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from . import i18n, log
from .decision import DecisionModel, LocalDecision, make_decision, noul
from .gitstore import Repo
from .llm import LLM, make_llm
from .ontology import (ASSUMPTION, CONFIRMED, CONFLICT, CONSTRAINT, DECISION, FINDING, INVALID, PROGRESS,
                       REQUEST, Claim, State, merge3)
from .respond import Responder
from .skills import Skill, default_skills

_log = log.get("agent")

RELATIONS = {
    "k1": "It adds a condition or information to the request in progress",
    "k2": "It changes a premise of the request in progress",
    "k3": "It is a separate request unrelated to the request in progress",
    "k4": "It asks to stop the request in progress",
    "k5": "It asks how far the request in progress has got",
}
NAMES = {"k1": "supplement", "k2": "correction", "k3": "independent", "k4": "cancel", "k5": "progress"}
DEFAULT_POLICY = "# thresholds are policy: versioned in git with the conversation\nrelation_confidence: 0.8\nintent_change: 0.7\n"


@dataclass
class Answer:
    kind: str            # interim | final
    text: str
    request: str         # request id that triggered it
    relation: str = ""   # k1..k5 when a follow-up triggered it
    source: str = ""     # branch@sha or main@sha the answer was written from
    commit: str = ""     # commit on the `answers` branch


@dataclass
class Task:
    n: int
    text: str
    skill: Skill
    base: str
    independent: bool = False
    status: str = "open"            # open | merged | cancelled
    requests: List[str] = field(default_factory=list)
    title: str = ""
    finalized: bool = False

    @property
    def req(self) -> str:
        return f"req.{self.n}"

    @property
    def branch(self) -> str:
        return f"req/{self.n}"


class Agent:
    def __init__(self, repo: str, llm: Union[str, LLM] = "offline", decision: Union[str, DecisionModel] = "local",
                 lang: str = "", skills: Optional[List[Skill]] = None,
                 on_answer: Optional[Callable[[Answer], None]] = None,
                 on_step: Optional[Callable[[str, str], None]] = None):
        # on_answer fires the moment an answer exists (before slow work that follows it);
        # on_step(branch, subject) fires after every work commit, so a UI can show it is alive.
        self.on_answer, self.on_step = on_answer, on_step
        self.repo = Repo(repo)
        self.repo.ensure_main(DEFAULT_POLICY)
        self.policy = _policy(self._read_policy())
        self.llm = make_llm(llm) if isinstance(llm, str) else llm
        self.decision = make_decision(decision) if isinstance(decision, str) else decision
        self.lang = i18n.detect(lang)
        self.skills = skills or default_skills(self.llm, self.lang)
        self.responder = Responder(self.llm, self.lang)
        self.tasks: List[Task] = []
        self._restore()
        main = self.repo.read_state("main")
        states = [main] + [self.repo.read_state(t.branch) for t in self.open_tasks()]
        self.n = max([_req_num(c.id) for st in states for c in st.of(REQUEST)] +
                     [int(b.split("/")[-1]) for b in self.repo.branches() if b.split("/")[-1].isdigit()] + [0])
        self.clock = max((st.clock() for st in states), default=0)
        self.pending: Optional[Dict[str, Any]] = None
        self.answers: List[Answer] = []
        self.lock = threading.RLock()
        _log.info("agent ready repo=%s llm=%s decision=%s lang=%s open=%d", self.repo.path, self.llm.name,
                  self.decision.name, self.lang, len(self.open_tasks()))

    # --- public -------------------------------------------------------------------
    def submit(self, text: str) -> List[Answer]:
        """A user message arrives (possibly while other requests are being processed)."""
        with self.lock:
            text = text.strip()
            if not text:
                return []
            if self.pending:
                return self._clarified(text)
            self.n += 1
            req = f"req.{self.n}"
            open_ = self.open_tasks()
            _log.info("submit %s (%d open): %s", req, len(open_), text)
            if not open_:
                return self._start(text, self.n, independent=False)[1]
            target, rel, conf = self._judge(text, open_)
            if conf < self.policy["relation_confidence"]:
                self.pending = {"text": text, "n": self.n, "target": target}
                return [self._say("interim", {"kind": "clarify", "target": target.title}, target, req, rel, conf,
                                  user_text=text)]
            return self._apply(rel, conf, text, self.n, target)

    def tick(self) -> List[Answer]:
        """Run one unit of work on every open request; merge the ones that are done."""
        with self.lock:
            out: List[Answer] = []
            for task in self.open_tasks():
                state = self.repo.read_state(task.branch)
                self.clock += 1
                claims = task.skill.next_step(state, task.req, self.clock)
                if claims:
                    redo = [c.id for c in claims if state.get(c.id) is not None and state.get(c.id).status == INVALID]
                    for c in claims:
                        state.put(c)
                    main = next((c for c in claims if c.type != PROGRESS), claims[0])
                    verb = "redo" if main.id in redo else {FINDING: "find", DECISION: "decide"}.get(main.type, "update")
                    subject = f"{main.id}: {verb} ({_short(main.value)})"
                    self.repo.commit_state(task.branch, state, subject, {
                        "Request": task.req, "Branch": task.branch, "Base": self.repo.short(task.base)})
                    self._step(task.branch, subject)
                else:
                    out += self._merge(task)
            return out

    def run(self, max_ticks: int = 200) -> List[Answer]:
        out: List[Answer] = []
        for _ in range(max_ticks):
            if not self.open_tasks():
                break
            out += self.tick()
        return out

    def open_tasks(self) -> List[Task]:
        return [t for t in self.tasks if t.status == "open"]

    def busy(self) -> bool:
        return bool(self.open_tasks())

    def history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Recent conversation turns (user messages and answers) read from git in chronological order."""
        if limit <= 0:
            return []
        with self.lock:
            if not self.repo.rev("answers"):
                return []
            req_texts: Dict[str, str] = {}
            refs = ["main"] + [t.branch for t in self.open_tasks()] + self.repo.branches("cancelled/req/")
            for ref in refs:
                for c in self.repo.read_state(ref).of(REQUEST):
                    if c.id not in req_texts and c.value is not None:
                        req_texts[c.id] = str(c.value)
            commits = list(reversed(self.repo.log("answers", n=limit)))
            if not commits:
                return []
            specs = [f"{sha}:{name}" for sha, _ in commits for name in ("answer.txt", "user.txt")]
            blobs = self.repo.read_blobs(specs)
            items: List[Dict[str, Any]] = []
            seen: set = set()
            max_n = 0
            for sha, body in commits:
                tr = _trailers(body)
                kind = tr.get("Answer", "")
                if kind not in ("interim", "final"):
                    continue
                req_field = tr.get("Request", "")
                req_ids = [r.strip() for r in req_field.split(",") if r.strip()]
                user_blob = (blobs.get(f"{sha}:user.txt") or "").rstrip("\n")
                for rid in sorted(req_ids, key=_req_num):
                    n = _req_num(rid)
                    if rid in seen or (n and n <= max_n):
                        continue
                    text = req_texts.get(rid) or (user_blob if len(req_ids) == 1 else "")
                    if text:
                        items.append({"type": "user", "n": n or None, "text": text})
                        seen.add(rid)
                        if n > max_n:
                            max_n = n
                rel_raw = tr.get("Relation", "")
                rel_code = rel_raw.split()[0] if rel_raw else ""
                rel = rel_code if rel_code in NAMES else ""
                ans_text = (blobs.get(f"{sha}:answer.txt") or "").rstrip("\n")
                items.append({
                    "type": "answer",
                    "kind": kind,
                    "text": ans_text,
                    "request": req_field,
                    "relation": rel,
                    "relation_name": NAMES.get(rel, ""),
                    "source": tr.get("Source", ""),
                    "commit": sha,
                })
            return items

    # --- steps --------------------------------------------------------------------
    def _route(self, text: str) -> Skill:
        for s in self.skills:
            if s.owns(text):
                return s
        return self.skills[-1]

    def _start(self, text: str, n: int, independent: bool) -> Tuple[Task, List[Answer]]:
        skill = self._route(text)
        base = self.repo.rev("main")
        task = Task(n, text, skill, base, independent, requests=[f"req.{n}"], title=skill.title(text))
        self.repo.branch(task.branch, "main")
        # say "started" before any slow work (interpret, steps), so the user is never left waiting in silence
        still = [t.title for t in self.open_tasks()]
        ack = self._say("interim", {"kind": "start", "started": task.title, "still_working": still}, task,
                        task.req, "k3" if independent else "", 1.0)
        state = self.repo.read_state(task.branch)
        self.clock += 1
        state.put(Claim(task.req, REQUEST, text, task.req, label=task.title, at=self.clock))
        for c in skill.interpret(text, task.req, state, self.clock):
            state.put(c)
        self.repo.commit_state(task.branch, state, f"{task.req}: open ({_short(text)})", {
            "Request": task.req, "Relation": "k3 independent" if independent else "",
            "Branch": task.branch, "Base": self.repo.short(base)})
        self._step(task.branch, f"{task.req}: open")
        self.tasks.append(task)
        return task, [ack]

    def _judge(self, text: str, open_: List[Task]) -> Tuple[Task, str, float]:
        touches: List[str] = []
        hint = None
        for i, t in enumerate(open_):
            claims = t.skill.interpret(text, "probe", self.repo.read_state(t.branch), 0)
            if claims and hint is None:
                hint = f"k{i + 1}"
            touches += [c.type for c in claims if c.type not in touches]
        state: Dict[str, Any] = {
            "in_progress": "\n".join(f"R{t.n}: {t.text} (claims: {_summary(self.repo.read_state(t.branch))})"
                                     for t in open_),
            "new": text, "touches": touches}
        questions: Dict[str, Any] = {"relation": {"type": "choice", "criteria": dict(RELATIONS),
            "instructions": "How does the new request relate to the request in progress?"}}
        if len(open_) > 1:
            questions["target"] = {"type": "choice", "instructions": "Which request in progress is the new request about?",
                                   "criteria": {f"k{i + 1}": f"R{t.n}: {t.text}" for i, t in enumerate(open_)}}
            if isinstance(self.decision, LocalDecision):
                state["target_hint"] = hint or f"k{len(open_)}"
        ans = self.decision.decide(state, questions)
        target = open_[-1]
        if "target" in ans:
            target = open_[int(ans["target"]["choice"][1:]) - 1]
        rel, conf = ans["relation"]["choice"], ans["relation"]["confidence"]
        _log.info("relation %s (%s) confidence %.2f target %s", rel, NAMES[rel], conf, target.branch)
        return target, rel, conf

    def _clarified(self, reply: str) -> List[Answer]:
        p, self.pending = self.pending, None
        target: Task = p["target"]
        ans = self.decision.decide({"question": f"Apply '{p['text']}' to R{target.n} or handle separately?",
                                    "new": reply}, {"clarify": {"type": "choice",
                                    "instructions": "What did the user answer?",
                                    "criteria": {"k1": "Apply it to the request in progress",
                                                 "k2": "Handle it as a separate request"}}})
        if ans["clarify"]["choice"] == "k1":
            touches = [c.type for c in target.skill.interpret(p["text"], "probe", self.repo.read_state(target.branch), 0)]
            rel = "k2" if ASSUMPTION in touches else "k1"
        else:
            rel = "k3"
        return self._apply(rel, ans["clarify"]["confidence"], p["text"], p["n"], target)

    def _apply(self, rel: str, conf: float, text: str, n: int, target: Task) -> List[Answer]:
        req = f"req.{n}"
        relation = f"{rel} {NAMES[rel]} (confidence {conf:.2f})"
        if rel == "k3":
            return self._start(text, n, independent=True)[1]
        state = self.repo.read_state(target.branch)
        if rel == "k5":
            return [self._say("interim", {"kind": "progress", **self._status_facts(target, state)}, target, req, rel,
                              conf, user_text=text)]
        self.clock += 1
        state.put(Claim(req, REQUEST, text, req, label=_short(text), at=self.clock))
        trailers = {"Request": req, "Relation": relation, "Branch": target.branch,
                    "Base": self.repo.short(target.base), "Decision-Model": self.decision.name}
        if rel == "k4":
            kept = [c.label or c.id for c in state if c.type in (FINDING, DECISION) and c.status == CONFIRMED]
            self.repo.commit_state(target.branch, state, f"{target.req}: cancel", trailers)
            self.repo.close(target.branch, f"cancelled/{target.branch}")
            target.status = "cancelled"
            return [self._say("interim", {"kind": "cancel", "stopped": target.title, "kept": _uniq(kept)},
                              target, req, rel, conf, source_ref=f"cancelled/{target.branch}")]
        claims = target.skill.interpret(text, req, state, self.clock)
        target.requests.append(req)
        if rel == "k2" and claims:
            changed, invalid = [], []
            for c in claims:
                old, inv = state.correct(c)
                invalid += inv
                if old is not None and old.value != c.value:
                    changed.append((c.label or c.id, old.value, c.value))
            subject = "; ".join(f"{l}: correct ({o} → {v})" for l, o, v in changed) or f"{req}: correct"
            subject = f"{claims[0].id}: correct ({changed[0][1]} → {changed[0][2]})" if changed else subject
            self.repo.commit_state(target.branch, state, subject, {**trailers, "Invalidates": ", ".join(invalid)})
            if target.skill.owns(text):
                target.title = target.skill.title(text)
            dropped = _uniq(state.get(i).label or i for i in invalid if state.get(i).type in (FINDING, DECISION))
            facts = {"kind": "correction", "changed": changed, "dropped": dropped,
                     "redoing": target.skill.pending(state)}
            return [self._say("interim", facts, target, req, rel, conf)]
        # k1 (and k2 that names no claim): add the claims, redo only what depends on them
        for c in claims:
            state.put(c)
        invalid = state.invalidate([c.id for c in claims]) if claims else []
        head = claims[0] if claims else None
        subject = f"{head.id}: add ({_short(head.value)})" if head else f"{req}: note"
        self.repo.commit_state(target.branch, state, subject, {**trailers, "Invalidates": ", ".join(invalid)})
        facts = {"kind": "supplement", "decided": self._decided(state),
                 "new": [(c.label or c.id, c.value) for c in claims] or [("", _short(text))],
                 "redoing": target.skill.pending(state)}
        return [self._say("interim", facts, target, req, rel, conf)]

    def _merge(self, task: Task) -> List[Answer]:
        base = self.repo.merge_base("main", task.branch)
        merged, conflicts = merge3(self.repo.read_state(base), self.repo.read_state("main"),
                                   self.repo.read_state(task.branch), self._intent_changed,
                                   self.policy["intent_change"])
        sha = self.repo.merge_commit(task.branch, merged, f"Merge {task.branch}: {task.title}", {
            "Request": ", ".join(task.requests), "Branch": task.branch, "Base": self.repo.short(base),
            "Conflicts": f"{len(conflicts)} ({sum(1 for c in conflicts if not c.resolved)} unresolved)" if conflicts else "",
            "Decision-Model": self.decision.name if conflicts else ""})
        task.status = "merged"
        out: List[Answer] = []
        if task.independent:
            ans = [(c.label or c.id, c.value) for c in merged
                   if c.from_ == task.req and c.type in (FINDING, DECISION) and c.status == CONFIRMED]
            facts = {"kind": "independent", "answer": ans,
                     "still_working": [t.title for t in self.open_tasks()]}
            out.append(self._say("interim", facts, task, task.req, "k3", 1.0, source_ref="main"))
        if not self.open_tasks():
            roots = [t for t in self.tasks if t.status == "merged" and not t.independent and not t.finalized]
            if roots:
                out.append(self._final(roots, merged, sha))
        return out

    def _final(self, roots: List[Task], merged: State, sha: str) -> Answer:
        reqs = {r for t in roots for r in t.requests}
        first = {t.req for t in roots}
        base = self.repo.read_state(roots[0].base)
        plan = [(c.label or c.id, c.value if c.status != CONFLICT else " / ".join(map(str, c.alternatives)))
                for c in merged if c.from_ in reqs and c.type == DECISION and c.status in (CONFIRMED, CONFLICT)]
        lookup = not plan  # a lookup ends in findings, not decisions ("what time is the meeting?")
        if lookup:
            plan = [(c.label or c.id, c.value) for c in merged
                    if c.from_ in reqs and c.type == FINDING and c.status == CONFIRMED]
        prov = []
        for c in merged:
            if c.from_ in reqs and c.from_ not in first and c.type in (CONSTRAINT, ASSUMPTION, DECISION, FINDING) \
                    and not c.same(base.get(c.id)):
                nth = i18n.ordinal(int(c.from_.split(".")[1]), i18n.template_lang(self.lang))
                prov.append((c.label or c.id, c.value, nth, "changed" if c.corrects else "added"))
        conflicts = [(c.label or c.id, c.alternatives[0], c.alternatives[-1]) for c in merged if c.status == CONFLICT]
        facts = {"kind": "final", "plan": plan, "lookup": lookup, "provenance": prov, "conflicts": conflicts,
                 "is_plan": any(t.skill.name == "trip" for t in roots),
                 "requests": [t.text for t in roots]}
        for t in roots:
            t.finalized = True
        return self._say("final", facts, roots[0], ", ".join(sorted(reqs)), "", 1.0, source_ref="main", sha=sha)

    # --- helpers ------------------------------------------------------------------
    def _decided(self, state: State) -> List[Tuple[str, Any]]:
        return [(c.label or c.id, c.value) for c in state if c.type == DECISION and c.status == CONFIRMED]

    def _status_facts(self, task: Task, state: State) -> Dict[str, Any]:
        return {"decided": self._decided(state),
                "found": [(c.label or c.id, c.value) for c in state if c.type == FINDING and c.status == CONFIRMED],
                "working_on": task.skill.pending(state)}

    def _intent_changed(self, earlier: Claim, later: Claim) -> float:
        return noul(self.decision, {"earlier": f"{earlier.from_}: {earlier.id} = {earlier.value}",
                                    "later": f"{later.from_}: {later.id} = {later.value}"},
                    "Does the later request change the intent of the earlier one?")

    def _say(self, kind: str, facts: Dict[str, Any], task: Task, req: str, rel: str, conf: float,
             source_ref: str = "", sha: str = "", user_text: str = "") -> Answer:
        ref = source_ref or task.branch
        sha = sha or self.repo.rev(ref) or ""
        facts = {**facts, "answer_type": kind}
        text = self.responder.write(facts)
        source = f"{ref}@{self.repo.short(sha)}"
        commit = self.repo.record_answer(kind, text, source, {
            "Request": req, "Relation": f"{rel} {NAMES[rel]} ({conf:.2f})" if rel else ""},
            user_text=user_text)
        ans = Answer(kind, text, req, rel, source, commit)
        self.answers.append(ans)
        _log.info("answer %s from %s: %s", kind, source, text)
        if self.on_answer:
            self.on_answer(ans)
        return ans

    def _step(self, branch: str, subject: str) -> None:
        _log.debug("step %s %s", branch, subject)
        if self.on_step:
            self.on_step(branch, subject)

    def _read_policy(self) -> str:
        try:
            return self.repo.git("show", "main:policy.yaml")
        except RuntimeError:
            return DEFAULT_POLICY

    def _restore(self) -> None:
        """Rebuild in-progress tasks from unmerged `req/*` branches so work resumes after restart."""
        req_branches: List[Tuple[int, str]] = []
        for b in self.repo.branches("req/"):
            parts = b.split("/")
            if len(parts) == 2 and parts[1].isdigit():
                req_branches.append((int(parts[1]), b))
        req_branches.sort()
        open_branches: List[Tuple[int, str]] = []
        merged_branches: List[Tuple[int, str]] = []
        for n, branch in req_branches:
            if self.repo.is_ancestor(branch, "main"):
                merged_branches.append((n, branch))
            else:
                open_branches.append((n, branch))
        if not open_branches:
            return
        finalized = self._finalized_requests()
        merge_bases = {p2: self.repo.merge_base(p1, p2) for _, p1, p2 in self.repo.merges("main")}
        for n, branch in merged_branches:
            if f"req.{n}" in finalized:
                continue
            sha = self.repo.rev(branch)
            base = merge_bases.get(sha or "")
            if not base:
                continue
            task = self._restore_task(n, branch, base, status="merged")
            if task and not task.independent:
                self.tasks.append(task)
        for n, branch in open_branches:
            task = self._restore_task(n, branch, self.repo.merge_base("main", branch), status="open")
            if task:
                self.tasks.append(task)
        self.tasks.sort(key=lambda t: t.n)
        open_ = self.open_tasks()
        if open_:
            _log.info("restored %d open task(s): %s", len(open_), [t.branch for t in open_])

    def _restore_task(self, n: int, branch: str, base: str, status: str = "open") -> Optional[Task]:
        commits = self.repo.log(f"{base}..refs/heads/{branch}", n=200)
        if not commits:
            return None
        base_state = self.repo.read_state(base)
        state = self.repo.read_state(branch)
        req_id = f"req.{n}"
        req_claims = sorted((c for c in state.of(REQUEST) if base_state.get(c.id) is None),
                            key=lambda c: (_req_num(c.id), c.at))
        primary = state.get(req_id) or (req_claims[0] if req_claims else None)
        if primary is not None:
            text = str(primary.value)
        else:
            subject = commits[-1][1].splitlines()[0] if commits[-1][1] else ""
            text = subject.partition(": open (")[2].rstrip(")") or subject
        requests = [c.id for c in req_claims]
        if req_id not in requests:
            requests.insert(0, req_id)
        open_trailers = _trailers(commits[-1][1])
        independent = open_trailers.get("Relation", "").startswith("k3")
        skill = self._route(text)
        title = skill.title(text)
        for _, body in reversed(commits[:-1]):
            subj = body.splitlines()[0] if body else ""
            tr = _trailers(body)
            if tr.get("Relation", "").startswith("k2") and not subj.endswith(": note"):
                c = state.get(tr.get("Request", ""))
                if c is not None and skill.owns(str(c.value)):
                    title = skill.title(str(c.value))
        return Task(n=n, text=text, skill=skill, base=base, independent=independent,
                    status=status, requests=requests, title=title)

    def _finalized_requests(self) -> set:
        done = set()
        if not self.repo.rev("answers"):
            return done
        for _, body in self.repo.log("answers", n=500):
            tr = _trailers(body)
            if tr.get("Answer") == "final":
                for r in tr.get("Request", "").split(","):
                    if r.strip():
                        done.add(r.strip())
        return done


def _trailers(body: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in body.splitlines()[1:]:
        if ": " in line:
            k, _, v = line.partition(": ")
            if k and not k[0].isspace():
                out[k.strip()] = v.strip()
    return out


def _req_num(cid: str) -> int:
    head, _, tail = cid.rpartition(".")
    return int(tail) if head == "req" and tail.isdigit() else 0


def _policy(text: str) -> Dict[str, float]:
    out = {"relation_confidence": 0.8, "intent_change": 0.7}
    for line in text.splitlines():
        k, _, v = line.partition(":")
        if k.strip() in out:
            try:
                out[k.strip()] = float(v)
            except ValueError:
                pass
    return out


def _short(v: Any, n: int = 48) -> str:
    s = str(v)
    return s if len(s) <= n else s[: n - 1] + "…"


def _uniq(items) -> List[str]:
    out: List[str] = []
    for i in items:
        if i not in out:
            out.append(i)
    return out


def _summary(state: State) -> str:
    return ", ".join(f"{c.id}={_short(c.value, 24)} [{c.status}]" for c in state
                     if c.type not in (REQUEST, PROGRESS)) or "none yet"
