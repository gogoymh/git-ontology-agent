"""Skills turn a request into claims, one step at a time.

A skill never re-runs work blindly: `next_step` recomputes the first claim of
its pipeline that is missing or invalid, so the dependency graph decides what
gets redone. Built-in offline skills drive the demo; `LLMSkill` handles any
request with a real model.
"""
from __future__ import annotations

import json
import re
from typing import List, Optional

from . import i18n, log
from .llm import LLM
from .ontology import (ASSUMPTION, CONFIRMED, CONSTRAINT, DECISION, FINDING, INVALID, PROGRESS,
                       TYPES, Claim, State)

_log = log.get("skills")


class Skill:
    name = "skill"

    def __init__(self, lang: str = "en"):
        self.lang = i18n.template_lang(lang)

    def owns(self, text: str) -> bool:
        return False

    def title(self, text: str) -> str:
        return text[:40]

    def interpret(self, text: str, req: str, state: State, at: int) -> List[Claim]:
        """Claims a new or follow-up request introduces (constraints, assumptions)."""
        return []

    def next_step(self, state: State, req: str, at: int) -> Optional[List[Claim]]:
        """Claims produced by the next unit of work, or None when the request is done."""
        return None

    def pending(self, state: State) -> List[str]:
        """Labels of the work still to do."""
        return []


def _t(lang: str, en: str, ko: str) -> str:
    return ko if lang == "ko" else en


# --- Trip planner (offline demo data) ------------------------------------------
CITIES = {
    "busan": {"names": ("busan", "부산"), "en": "Busan", "ko": "부산", "station": ("Busan", "부산"),
              "depart": "08:00", "train": 120000, "extras": 160000,
              "hotels": [("C", 420000, 4.8), ("B", 260000, 4.6), ("A", 180000, 4.5)]},
    "daegu": {"names": ("daegu", "대구"), "en": "Daegu", "ko": "대구", "station": ("Dongdaegu", "동대구"),
              "depart": "08:30", "train": 90000, "extras": 140000,
              "hotels": [("B", 160000, 4.7), ("F", 210000, 4.5), ("G", 140000, 4.1)]},
}
_NEG = r"(?:(\S+?)\s*(?:말고|아니라|아니고))|(?:\bnot\s+(\w+))|(?:instead of\s+(\w+))"


def parse_money(text: str) -> Optional[int]:
    t = text.lower().replace(",", "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*만\s*원?", t)
    if m:
        return int(float(m.group(1)) * 10000)
    m = re.search(r"(?:₩|krw\s*)(\d+)|(\d+)\s*(?:won|krw|원)", t)
    if m:
        return int(m.group(1) or m.group(2))
    return None


class TripSkill(Skill):
    name = "trip"
    PIPELINE = ("plan.train", "hotel.candidates", "plan.hotel", "plan.total")

    def owns(self, text):
        t = text.lower()
        return bool(re.search(r"출장|여행|trip|travel|itinerary", t)) or self._city(t) is not None

    def title(self, text):
        city = self._city(text.lower()) or "busan"
        return _t(self.lang, f"{CITIES[city]['en']} trip plan", f"{CITIES[city]['ko']} 출장 일정")

    def _city(self, t: str) -> Optional[str]:
        negated = {g.lower() for m in re.finditer(_NEG, t) for g in m.groups() if g}
        hits = []
        for key, c in CITIES.items():
            for n in c["names"]:
                i = t.find(n)
                if i >= 0 and not any(n in neg for neg in negated):
                    hits.append((i, key))
        return min(hits)[1] if hits else None

    def label(self, cid: str) -> str:
        en = {"trip.city": "trip city", "constraint.budget": "budget", "plan.train": "KTX",
              "hotel.candidates": "hotel candidates", "plan.hotel": "hotel", "plan.total": "total"}
        ko = {"trip.city": "출장지", "constraint.budget": "예산", "plan.train": "KTX",
              "hotel.candidates": "숙소 후보", "plan.hotel": "숙소", "plan.total": "합계"}
        return (ko if self.lang == "ko" else en).get(cid, cid)

    def interpret(self, text, req, state, at):
        t, out = text.lower(), []
        city = self._city(t)
        if city:
            out.append(Claim("trip.city", ASSUMPTION, CITIES[city][self.lang], req,
                             label=self.label("trip.city"), at=at))
        budget = parse_money(t)
        if budget:
            out.append(Claim("constraint.budget", CONSTRAINT, i18n.money(budget, self.lang), req,
                             label=self.label("constraint.budget"), at=at))
        return out

    # helpers on current state
    def _city_of(self, state: State) -> dict:
        c = state.get("trip.city")
        for data in CITIES.values():
            if c and c.value in (data["en"], data["ko"]):
                return data
        return CITIES["busan"]

    def _budget(self, state: State) -> Optional[int]:
        c = state.get("constraint.budget")
        vals = c.value if c and isinstance(c.value, list) else [c.value] if c else []
        nums = [parse_money(str(v)) for v in vals]
        nums = [n for n in nums if n]
        return min(nums) if nums else None

    def _hotels(self, state: State):
        city, budget = self._city_of(state), self._budget(state)
        hotels = sorted(city["hotels"], key=lambda h: -h[2])
        if budget:
            room = budget - city["train"] - city["extras"]
            hotels = [h for h in hotels if h[1] <= room]
        return hotels[:3]

    def _hotel_name(self, letter: str) -> str:
        return _t(self.lang, f"Hotel {letter}", f"숙소 {letter}")

    def next_step(self, state, req, at):
        todo = [cid for cid in self.PIPELINE if state.get(cid) is None or state.get(cid).status == INVALID]
        if not todo:
            return None
        cid, city, lang = todo[0], self._city_of(state), self.lang
        mk = lambda typ, value, deps: Claim(cid, typ, value, req, CONFIRMED, deps, self.label(cid), at)  # noqa: E731
        if cid == "plan.train":
            frm, to = _t(lang, "Seoul", "서울"), city["station"][1 if lang == "ko" else 0]
            claim = mk(DECISION, _t(lang, f"Tue {city['depart']} KTX ({frm}→{to})",
                                    f"화 {city['depart']} KTX({frm}→{to})"), ["trip.city"])
        elif cid == "hotel.candidates":
            names = [self._hotel_name(h[0]) for h in self._hotels(state)]
            claim = mk(FINDING, ", ".join(names) or _t(lang, "none within budget", "예산 안 숙소 없음"),
                       ["trip.city", "constraint.*"])
        elif cid == "plan.hotel":
            hotels = self._hotels(state)
            if hotels:
                h = hotels[0]
                v = _t(lang, f"{self._hotel_name(h[0])} (2 nights, {i18n.money(h[1], lang)})",
                       f"{self._hotel_name(h[0])}(2박 {i18n.money(h[1], lang)})")
            else:
                v = _t(lang, "no hotel fits", "맞는 숙소 없음")
            claim = mk(DECISION, v, ["hotel.candidates", "constraint.*"])
        else:
            hotels = self._hotels(state)
            total = city["train"] + city["extras"] + (hotels[0][1] if hotels else 0)
            budget = self._budget(state)
            v = _t(lang, "total ", "합계 ") + i18n.money(total, lang)
            if budget:
                ok = total <= budget
                v += _t(lang, f" ({'within' if ok else 'over'} the {i18n.money(budget, lang)} budget)",
                        f"(예산 {i18n.money(budget, lang)} {'안' if ok else '초과'})")
            claim = mk(DECISION, v, ["plan.train", "plan.hotel", "constraint.*"])
        _log.info("trip step %s -> %s", cid, claim.value)
        return [claim]

    def pending(self, state):
        return [self.label(c) for c in self.PIPELINE
                if state.get(c) is None or state.get(c).status == INVALID]


# --- Calendar (offline demo data) ---------------------------------------------
class CalendarSkill(Skill):
    name = "calendar"

    def owns(self, text):
        return bool(re.search(r"회의|미팅|meeting|calendar|캘린더", text.lower()))

    def title(self, text):
        return _t(self.lang, "tomorrow's meeting", "내일 회의")

    def next_step(self, state, req, at):
        if state.get("meeting.tomorrow"):
            return None
        return [Claim("meeting.tomorrow", FINDING, _t(self.lang, "at 2:00 PM", "오후 2시"), req,
                      label=_t(self.lang, "Tomorrow's meeting", "내일 회의"), at=at)]

    def pending(self, state):
        return [] if state.get("meeting.tomorrow") else [self.title("")]


# --- Any request, with a real LLM ------------------------------------------------
_CLAIM_SCHEMA = {
    "type": "object", "required": ["id", "type", "value", "label", "depends_on"],
    "properties": {"id": {"type": "string"}, "type": {"type": "string", "enum": list(TYPES[1:])},
                   "value": {"type": "string"}, "label": {"type": "string"},
                   "depends_on": {"type": "array", "items": {"type": "string"}}}}
_STEP_SCHEMA = {"type": "object", "required": ["done", "claims", "remaining"], "properties": {
    "done": {"type": "boolean"}, "claims": {"type": "array", "items": _CLAIM_SCHEMA},
    "remaining": {"type": "array", "items": {"type": "string"}}}}
_SYS = ("You are the worker of a git-backed agent. Work state is a list of typed claims: Constraint "
        "(condition to keep), Assumption (premise), Finding (checked fact), Decision (chosen result). "
        "Use dotted ids like trip.city. Each claim lists the ids it depends on (a trailing .* matches a "
        "prefix). Write labels and values in language '{lang}'.")


class LLMSkill(Skill):
    name = "llm"

    def __init__(self, llm: LLM, lang: str = "en"):
        super().__init__(lang)
        self.llm, self.full_lang = llm, lang

    def owns(self, text):
        return True

    def _claims(self, raw, req, at) -> List[Claim]:
        out = []
        for c in raw or []:
            if c.get("type") not in TYPES[1:]:
                continue
            out.append(Claim(str(c["id"]), c["type"], c.get("value"), req, CONFIRMED,
                             list(c.get("depends_on") or []), c.get("label", ""), at))
        return out

    def title(self, text):
        return text if len(text) <= 40 else text[:39] + "…"

    def interpret(self, text, req, state, at):
        prompt = (f"Current claims:\n{state.to_yaml()}\nNew message: {text}\n"
                  "Return only the Constraint or Assumption claims this message states or changes. "
                  "Reuse an existing id when it changes that claim.")
        data = self.llm.json(_SYS.format(lang=self.full_lang), prompt,
                             {"type": "object", "required": ["claims"],
                              "properties": {"claims": {"type": "array", "items": _CLAIM_SCHEMA}}})
        return [c for c in self._claims(data.get("claims"), req, at) if c.type in (CONSTRAINT, ASSUMPTION)]

    def next_step(self, state, req, at):
        reqs = [c for c in state if c.type == "Request"]
        prompt = ("Requests:\n" + "\n".join(f"- {c.id}: {c.value}" for c in reqs) +
                  f"\nCurrent claims:\n{state.to_yaml()}\n"
                  "Do the next single unit of work. Recompute claims whose status is invalid first. "
                  "Return 1-3 new or recomputed claims, the labels of what remains, and done=true only "
                  "when nothing is missing or invalid.")
        data = self.llm.json(_SYS.format(lang=self.full_lang), prompt, _STEP_SCHEMA, 1500)
        claims = self._claims(data.get("claims"), req, at)
        remaining = [str(x) for x in data.get("remaining") or []]
        invalid_left = [c for c in state if c.status == INVALID and c.id not in {x.id for x in claims}]
        if data.get("done") and not claims and not invalid_left:
            return None
        claims.append(Claim("progress.remaining", PROGRESS, json.dumps(remaining, ensure_ascii=False), req,
                            label="remaining", at=at))
        return claims

    def pending(self, state):
        p = state.get("progress.remaining")
        try:
            return json.loads(p.value) if p else []
        except ValueError:
            return []


def default_skills(llm: LLM, lang: str) -> List[Skill]:
    if llm.offline:
        return [TripSkill(lang), CalendarSkill(lang)]
    return [LLMSkill(llm, lang)]
