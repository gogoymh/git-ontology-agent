"""Pick the Korean particle that fits the sound a word ends with (polish SKILL.md section 3).

    pick("GOA-14", "을") -> "를"     pick("GOA-17", "으로") -> "로"     pick("Claude", "이") -> "가"

final(word) returns None (no final consonant), "ㄹ" or "C" (any other final consonant).
Hangul syllables use (code - 0xAC00) % 28; numbers use how the last digit (or 10, 100, ...) is read;
English words use a reading table, letter names for acronyms, then a spelling heuristic.
"""
import re

# Pairs: (form after a final consonant, form after a vowel). 으로/로 also takes 로 after ㄹ.
PAIRS = [("을", "를"), ("이", "가"), ("은", "는"), ("과", "와"), ("으로", "로")]
FORMS = {f: p for p in PAIRS for f in p}

# 0 영, 1 일, 2 이, 3 삼, 4 사, 5 오, 6 육, 7 칠, 8 팔, 9 구
DIGIT = {"0": "C", "1": "ㄹ", "2": None, "3": "C", "4": None, "5": None, "6": "C", "7": "ㄹ", "8": "ㄹ", "9": None}
# A 에이 B 비 C 씨 D 디 E 이 F 에프 G 지 H 에이치 I 아이 J 제이 K 케이 L 엘 M 엠 N 엔 O 오 P 피 Q 큐 R 알
# S 에스 T 티 U 유 V 브이 W 더블유 X 엑스 Y 와이 Z 지
LETTER = {c: None for c in "ABCDEFGHIJKOPQSTUVWXYZ"}
LETTER.update({"L": "ㄹ", "M": "C", "N": "C", "R": "ㄹ"})
# Words whose spelling misleads the heuristic, with their reading.
READING = {
    "antigravity": "안티그래비티", "claude": "클로드", "codex": "코덱스", "grok": "그록", "muse": "뮤즈",
    "readme": "리드미", "github": "깃허브", "git": "깃", "commit": "커밋", "main": "메인", "serve": "서브",
    "connect": "커넥트", "tasks": "태스크스", "docs": "독스", "history": "히스토리", "answers": "앤서스",
    "preconquer": "프리컨커", "slack": "슬랙", "notion": "노션", "jira": "지라", "python": "파이썬",
}


def hangul_final(ch):
    j = (ord(ch) - 0xAC00) % 28
    return None if j == 0 else ("ㄹ" if j == 8 else "C")


def english_final(w):
    low = w.lower()
    if low in READING:
        return hangul_final(READING[low][-1])
    if w.isupper() and len(w) <= 5:  # acronym read letter by letter: CI 씨아이, PR 피알
        return LETTER[w[-1]]
    if low.endswith(("ng", "m", "n")):
        return "C"
    if low.endswith(("l", "le")):
        return "ㄹ"
    if low.endswith(("ck", "k", "p")):
        return "C"
    if low.endswith("t") and len(low) > 1 and low[-2] in "aeiou":  # chat 챗, commit 커밋; test 테스트
        return "C"
    return None  # vowels, silent e, and endings read with 으: Claude 클로드, bug 버그


def final(word):
    """Final sound of the last readable part of word: None, 'ㄹ' or 'C'."""
    w = re.sub(r"[^0-9A-Za-z가-힣]+$", "", word)
    if not w:
        raise ValueError("no readable sound in %r" % word)
    ch = w[-1]
    if "가" <= ch <= "힣":
        return hangul_final(ch)
    if ch.isdigit():
        digits = re.search(r"[0-9]+$", w).group()
        if ch != "0" or digits == "0":
            return DIGIT[ch]
        return "C"  # 10 십, 100 백, 1000 천, 10000 만 all end in a consonant other than ㄹ
    return english_final(re.search(r"[A-Za-z]+$", w).group())


def pick(word, particle):
    """Return the form of particle (any member of a pair) that fits after word."""
    hard, soft = FORMS[particle]
    f = final(word)
    if f is None or (hard == "으로" and f == "ㄹ"):
        return soft
    return hard


# Ticket numbers (GOA-14) and English names; a bare number is skipped so '4가지' is not read as 4 + 가.
# Longest first so 으로 wins over 로.
ATTACHED = re.compile(r"(?<![0-9A-Za-z_/*.\-])([A-Za-z][A-Za-z0-9_/*.\-]*)(으로|로|을|를|이|가|은|는|과|와)")


def mismatches(line):
    """(token, written, expected) for every ticket number or English name with a particle attached."""
    out = []
    for m in ATTACHED.finditer(line):
        token, written = m.group(1), m.group(2)
        if not re.search(r"[0-9A-Za-z]$", token):
            continue  # req/* and the like: no sound to read
        want = pick(token, written)
        if want != written:
            out.append((token, written, want))
    return out


def attached(line):
    return [m.group(1) for m in ATTACHED.finditer(line) if re.search(r"[0-9A-Za-z]$", m.group(1))]
