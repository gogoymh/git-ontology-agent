"""The ontology: typed claims, the dependency graph, and per-type merge rules.

git knows when, who and what changed. The ontology knows what depends on what
and how to merge it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from . import log

_log = log.get("ontology")

# Claim types
REQUEST, CONSTRAINT, ASSUMPTION, FINDING, DECISION, PROGRESS = (
    "Request", "Constraint", "Assumption", "Finding", "Decision", "Progress")
TYPES = (REQUEST, CONSTRAINT, ASSUMPTION, FINDING, DECISION, PROGRESS)

# Claim statuses and how an answer phrases them
CONFIRMED, IN_PROGRESS, INVALID, SUPERSEDED, CONFLICT = (
    "confirmed", "in_progress", "invalid", "superseded", "conflict")
STATUSES = (CONFIRMED, IN_PROGRESS, INVALID, SUPERSEDED, CONFLICT)

STATE_FILE = "state.yaml"


@dataclass
class Claim:
    id: str
    type: str
    value: object = None
    from_: str = ""
    status: str = CONFIRMED
    depends_on: List[str] = field(default_factory=list)
    label: str = ""
    at: int = 0                      # logical clock: later wins for Findings
    corrects: str = ""               # request whose value this claim corrected
    prev: object = None              # value before the correction
    alternatives: List[object] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.type not in TYPES:
            raise ValueError(f"unknown claim type {self.type!r}")
        if self.status not in STATUSES:
            raise ValueError(f"unknown claim status {self.status!r}")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["from"] = d.pop("from_")
        # keep state.yaml short: drop empty optional fields
        return {k: v for k, v in d.items() if v not in (None, "", [], 0) or k in ("id", "type", "status")}

    @classmethod
    def from_dict(cls, d: dict) -> "Claim":
        d = dict(d)
        d["from_"] = d.pop("from", "")
        return cls(**d)

    def same(self, other: Optional["Claim"]) -> bool:
        return other is not None and self.to_dict() == other.to_dict()


def _matches(pattern: str, claim_id: str) -> bool:
    if pattern.endswith(".*"):
        return claim_id.startswith(pattern[:-1])
    return pattern == claim_id


class State:
    """An ordered set of claims. Serialized as YAML whose items are JSON flow maps."""

    def __init__(self, claims: Iterable[Claim] = ()):
        self.claims: Dict[str, Claim] = {}
        for c in claims:
            self.claims[c.id] = c

    # --- access -----------------------------------------------------------
    def get(self, cid: str) -> Optional[Claim]:
        return self.claims.get(cid)

    def put(self, claim: Claim) -> None:
        self.claims[claim.id] = claim

    def __iter__(self):
        return iter(self.claims.values())

    def __len__(self) -> int:
        return len(self.claims)

    def of(self, *types: str) -> List[Claim]:
        return [c for c in self if c.type in types]

    def clock(self) -> int:
        return max((c.at for c in self), default=0)

    def copy(self) -> "State":
        return State(Claim.from_dict(c.to_dict()) for c in self)

    # --- dependency graph -------------------------------------------------
    def dependents(self, changed: Iterable[str]) -> List[str]:
        """Every claim that transitively depends on any changed id, in state order."""
        frontier, hit = list(changed), set()
        while frontier:
            cur = frontier.pop()
            for c in self:
                if c.id in hit or c.id == cur:
                    continue
                if any(_matches(p, cur) for p in c.depends_on):
                    hit.add(c.id)
                    frontier.append(c.id)
        return [c.id for c in self if c.id in hit]

    def invalidate(self, changed: Iterable[str]) -> List[str]:
        ids = [i for i in self.dependents(changed) if self.claims[i].type not in (REQUEST, PROGRESS)]
        for i in ids:
            self.claims[i].status = INVALID
        _log.info("invalidated %s after change to %s", ids, list(changed))
        return ids

    def correct(self, claim: Claim) -> Tuple[Optional[Claim], List[str]]:
        """Retract the old value of claim.id, commit the new one, invalidate dependents."""
        old = self.get(claim.id)
        if old is not None:
            hist = Claim.from_dict(old.to_dict())
            hist.id = f"{old.id}#{old.from_ or 'base'}"
            hist.status = SUPERSEDED
            hist.depends_on = []
            self.put(hist)
            claim.prev = old.value
            claim.corrects = old.from_
        self.put(claim)
        return old, self.invalidate([claim.id])

    # --- serialization ----------------------------------------------------
    def to_yaml(self) -> str:
        lines = ["# Git Ontology Agent state: one claim per line"]
        for c in self:
            lines.append("- " + json.dumps(c.to_dict(), ensure_ascii=False, sort_keys=False))
        return "\n".join(lines) + "\n"

    @classmethod
    def from_yaml(cls, text: str) -> "State":
        claims = []
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("- "):
                claims.append(Claim.from_dict(json.loads(line[2:])))
        return cls(claims)


# --- merge ------------------------------------------------------------------
@dataclass
class Conflict:
    id: str
    ours: Claim
    theirs: Claim
    resolved: str = ""   # "", "theirs", "ours"
    score: float = 0.0

    def describe(self) -> str:
        return f"{self.id}: {self.ours.value!r} ({self.ours.from_}) vs {self.theirs.value!r} ({self.theirs.from_})"


# A judge answers "does the later request change the intent of the earlier one?" in [0, 1].
Judge = Callable[[Claim, Claim], float]
NOUL_THRESHOLD = 0.7


def merge3(base: State, ours: State, theirs: State, judge: Optional[Judge] = None,
           threshold: float = NOUL_THRESHOLD) -> Tuple[State, List[Conflict]]:
    """Claim-level 3-way merge of `theirs` (a request branch) into `ours` (main).

    One side changed, or both changed alike: take it. Both changed differently:
    apply the merge rule of the claim type. What no rule settles is decided by
    the judge; below the threshold both values stay and the claim is marked
    `conflict` so the final answer asks the user.
    """
    out = State()
    conflicts: List[Conflict] = []
    order = list(ours.claims) + [i for i in theirs.claims if i not in ours.claims]
    for cid in order:
        b, o, t = base.get(cid), ours.get(cid), theirs.get(cid)
        if t is not None and t.type == PROGRESS:
            continue                                   # Progress never leaves its branch
        if o is not None and (t is None and b is None or o.same(t)):
            out.put(o); continue
        if o is None and t is not None:
            if b is None or not b.same(t):
                out.put(t)                             # added (or changed) only on the branch
            continue                                   # deleted on main, untouched on branch
        if t is None:
            if b is None or not b.same(o):
                out.put(o)                             # main changed, branch deleted: keep main
            continue
        if o.same(b):
            out.put(t); continue
        if t.same(b):
            out.put(o); continue
        out.put(_rule(o, t, conflicts, judge, threshold))
    _log.info("merged %d claims, %d conflicts (%d unresolved)", len(out), len(conflicts),
              sum(1 for c in conflicts if not c.resolved))
    return out, conflicts


def _rule(o: Claim, t: Claim, conflicts: List[Conflict], judge: Optional[Judge], threshold: float) -> Claim:
    kind = t.type
    if kind == REQUEST:
        return t
    if kind == CONSTRAINT:                             # constraints stack: keep all of them
        vals = []
        for v in (o.value, t.value):
            for x in (v if isinstance(v, list) else [v]):
                if x not in vals:
                    vals.append(x)
        merged = Claim.from_dict(t.to_dict())
        merged.value = vals if len(vals) > 1 else vals[0]
        return merged
    if kind == FINDING:                                # the later finding wins
        return t if t.at >= o.at else o
    if kind == ASSUMPTION and (o.corrects or t.corrects) and not (o.corrects and t.corrects):
        return t if t.corrects else o                  # the correcting side wins
    if kind == DECISION and (o.status == INVALID or t.status == INVALID):
        return t if o.status == INVALID else o         # recomputed side wins
    # Independent requests changed the same Assumption/Decision differently.
    later, earlier = (t, o) if t.at >= o.at else (o, t)
    c = Conflict(t.id, o, t)
    c.score = judge(earlier, later) if judge else 0.0
    conflicts.append(c)
    if c.score >= threshold:
        c.resolved = "theirs" if later is t else "ours"
        return later
    held = Claim.from_dict(later.to_dict())
    held.status = CONFLICT
    held.alternatives = [earlier.value, later.value]
    return held
