"""Language detection and small locale helpers (Korean particles, ordinals, money)."""
from __future__ import annotations

import locale
import os

SUPPORTED = ("en", "ko")


def detect(explicit: str = "") -> str:
    """GOA_LANG > explicit > system locale. Unknown languages render with English templates;
    an online LLM still writes the answer in that language."""
    for cand in (explicit, os.environ.get("GOA_LANG", ""), os.environ.get("LC_ALL", ""),
                 os.environ.get("LANG", ""), (locale.getlocale()[0] or "")):
        if cand:
            return cand.split(".")[0].split("_")[0].split("-")[0].lower() or "en"
    return "en"


def template_lang(lang: str) -> str:
    return lang if lang in SUPPORTED else "en"


def _has_batchim(word: str) -> bool:
    for ch in reversed(word.strip()):
        if "가" <= ch <= "힣":
            return (ord(ch) - 0xAC00) % 28 != 0
        if ch.isdigit():
            return ch in "013678"
        if ch.isalpha():
            return ch.lower() in "lmnr"
    return False


def josa(word: str, pair: str) -> str:
    """josa('회의', '은/는') -> '회의는'."""
    a, b = pair.split("/")
    return word + (a if _has_batchim(word) else b)


def ordinal(n: int, lang: str) -> str:
    if lang == "ko":
        names = ["첫", "두", "세", "네", "다섯", "여섯", "일곱", "여덟", "아홉", "열"]
        return (names[n - 1] if 0 < n <= len(names) else str(n)) + " 번째"
    names = ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth"]
    return names[n - 1] if 0 < n <= len(names) else f"{n}th"


def money(won: int, lang: str) -> str:
    if lang == "ko":
        return f"{won // 10000}만원" if won % 10000 == 0 else f"{won:,}원"
    return f"₩{won:,}"


def join(items, lang: str) -> str:
    items = list(items)
    if lang == "ko" or len(items) < 2:
        return ", ".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]
