"""Answer writing. Facts come from git (branch head or merge commit); the claim status picks the phrasing:

confirmed -> stated as decided, in_progress -> "working on", invalid -> never said,
superseded -> what changed and which request changed it.
An online LLM writes the prose; offline (or on LLM failure) the templates below do.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from . import i18n, log
from .llm import LLM

_log = log.get("respond")

RULES = (
    "Write a short chat answer for the user in language '{lang}'. Use only the facts given. "
    "Say confirmed facts plainly. Say in-progress work as ongoing; never present it as settled. "
    "Never mention invalid claims. For changes, say what changed and which request (by ordinal) changed it. "
    "Interim answers make clear the work is still going. Final answers give the complete result, name "
    "which later request added or changed each part, and ask the user to pick when a conflict is listed. "
    "No headings, no bullet lists, at most three sentences."
)


# Room for reasoning tokens, which count against the limit; the LLM retries with more on truncation.
ANSWER_TOKENS = 1024


class Responder:
    def __init__(self, llm: LLM, lang: str):
        self.llm, self.lang, self.t = llm, lang, i18n.template_lang(lang)

    def write(self, facts: Dict[str, Any]) -> str:
        if not self.llm.offline:
            try:
                text = self.llm.complete(RULES.format(lang=self.lang), json.dumps(facts, ensure_ascii=False), None,
                                         ANSWER_TOKENS)
                if text.strip():
                    return text.strip()
            except Exception as e:  # keep answering even when the provider is down
                _log.warning("LLM answer failed, using template: %s", e)
        return self.template(facts)

    # --- templates ------------------------------------------------------------
    def template(self, f: Dict[str, Any]) -> str:
        return getattr(self, "_" + f["kind"].replace("-", "_"))(f, self.t == "ko")

    def _pairs(self, pairs, ko: bool) -> List[str]:
        if ko:
            return [f"{i18n.josa(l, '은/는')} {v}" for l, v in pairs]
        return [f"{l}: {v}" for l, v in pairs]

    def _decided_sentence(self, f, ko) -> str:
        d = f.get("decided") or []
        if not d:
            return ""
        if ko:
            return " ".join(f"{i18n.josa(l, '은/는')} {v}로 정했습니다." for l, v in d)
        return " ".join(f"{l} is set: {v}." for l, v in d)

    def _supplement(self, f, ko):
        new = i18n.join((f"{l} {v}" if ko else f"{l} of {v}" for l, v in f["new"]), self.t)
        redo = i18n.join(f.get("redoing") or [], self.t)
        head = self._decided_sentence(f, ko)
        if ko:
            tail = f"말씀하신 {new} 조건을 반영해 {i18n.josa(redo, '을/를')} 다시 고르는 중입니다." if redo else f"말씀하신 {new} 조건을 반영했습니다."
        else:
            tail = f"Applying the {new} you mentioned, I'm redoing the {redo}." if redo else f"Noted the {new}."
        return (head + " " + tail).strip()

    def _correction(self, f, ko):
        ch = f["changed"]
        redo = i18n.join(f.get("redoing") or [], self.t)
        old = i18n.join(f.get("dropped") or [], self.t)
        if ko:
            s = " ".join(f"{i18n.josa(l, '을/를')} {n}로 바꿨습니다." for l, o, n in ch)
            if redo:
                s += f" {ch[0][1]} 기준으로 찾던 {i18n.josa(old or redo, '은/는')} 버리고 {ch[0][2]} 기준으로 다시 찾고 있습니다."
            return s
        s = " ".join(f"Changed the {l} to {n}." for l, o, n in ch)
        if redo:
            s += f" Dropping the {old or redo} found for {ch[0][1]} and redoing them for {ch[0][2]}."
        return s

    def _independent(self, f, ko):
        ans = " ".join(f"{i18n.josa(l, '은/는')} {v}입니다." if ko else f"{l} is {v}." for l, v in f["answer"])
        still = i18n.join(f.get("still_working") or [], self.t)
        if still:
            ans += f" {i18n.josa(still, '은/는')} 계속 짜고 있습니다." if ko else f" Still working on the {still}."
        return ans

    def _progress(self, f, ko):
        head = self._decided_sentence(f, ko)
        found = f.get("found") or []
        work = i18n.join(f.get("working_on") or [], self.t)
        if ko:
            parts = [head]
            if found:
                parts.append(" ".join(f"{i18n.josa(l, '은/는')} {v}까지 찾았습니다." for l, v in found))
            if work:
                parts.append(f"지금 {i18n.josa(work, '을/를')} 진행하는 중입니다.")
        else:
            parts = [head] + [f"Found {l}: {v}." for l, v in found]
            if work:
                parts.append(f"Now working on the {work}.")
        return " ".join(p for p in parts if p).strip()

    def _cancel(self, f, ko):
        kept = i18n.join(f.get("kept") or [], self.t)
        if ko:
            s = f"{i18n.josa(f['stopped'], '을/를')} 멈췄습니다."
            return s + (f" 지금까지 찾은 {i18n.josa(kept, '은/는')} 기록으로만 남겨 두었습니다." if kept else "")
        return f"Stopped the {f['stopped']}." + (f" What I found so far ({kept}) stays in history only." if kept else "")

    def _clarify(self, f, ko):
        if ko:
            return f"지금 하던 {f['target']}에 반영할까요, 별도로 처리할까요?"
        return f"Should I apply this to the {f['target']} in progress, or handle it separately?"

    def _final(self, f, ko):
        plan = " → ".join(v for _, v in f["plan"])
        out = (f"일정입니다. {plan}." if ko and f.get("is_plan") else
               f"결과입니다. {plan}." if ko else f"Here is the result: {plan}.")
        if not ko and f.get("is_plan"):
            out = f"Here is your plan: {plan}."
        for label, value, nth, how in f.get("provenance") or []:
            if ko:
                out += (f" {i18n.josa(label, '은/는')} {nth} 요청에서 {value}로 바꿨습니다." if how == "changed"
                        else f" {label} 조건은 {nth} 요청에서 반영했습니다.")
            else:
                out += (f" The {label} was changed to {value} in your {nth} request." if how == "changed"
                        else f" The {label} ({value}) came from your {nth} request.")
        for label, a, b in f.get("conflicts") or []:
            out += (f" {i18n.josa(label, '이/가')} 두 요청에서 다르게 나왔습니다. {a}와 {b} 중 어느 쪽으로 할까요?" if ko
                    else f" Your requests disagree on the {label}: {a} or {b}?")
        return out
