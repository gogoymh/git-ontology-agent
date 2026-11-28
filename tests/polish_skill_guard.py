"""Mutation checks for tests/polish_skill_check.py (mission criterion; not a unittest).

Each mutant breaks a copy of the polish skill; the check must reject it.

    python3 tests/polish_skill_guard.py unpaired|middot
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / ".claude" / "skills" / "polish" / "SKILL.md"
CHECK = ROOT / "tests" / "polish_skill_check.py"
MODES = ["frontmatter", "start", "list", "particle", "meaning", "middot"]


def fail(msg):
    sys.exit("polish_skill_guard: " + msg)


def passing(text, modes):
    """Modes of polish_skill_check.py that pass on text."""
    fd, path = tempfile.mkstemp(prefix="pq-polish-guard.", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        return [m for m in modes
                if subprocess.run([sys.executable, str(CHECK), m, path], capture_output=True).returncode == 0]
    finally:
        os.unlink(path)


def mutate(text, old, new):
    if old not in text:
        fail("mutation does not apply: " + old.strip()[:40])
    return text.replace(old, new, 1)


text = SKILL.read_text(encoding="utf-8")
mode = sys.argv[1] if len(sys.argv) > 1 else ""
if mode == "unpaired":
    mutants = {
        "last fix removed": mutate(text, "- 후: GOA-23을 맡았습니다.\n", ""),
        "first original removed": mutate(text, "- 전: 미션 GOA-11(으)로 맡았습니다.", "미션 GOA-11(으)로 맡았습니다."),
        "middle fix removed": mutate(text, "- 후: GOA-14를 마치고 병합했습니다.\n", ""),
        "fix left empty": mutate(text, "- 후: GOA-17을 끝내지 못하고", "- 후:\n\nGOA-17을 끝내지 못하고"),
        "two fixes in a row": mutate(text, "- 전: 미션 GOA-23(으)로 맡았습니다.", "- 후: GOA-23은 이미 맡았습니다."),
    }
    modes = [m for m in MODES if m != "middot"]
elif mode == "middot":
    mutants = {
        "rule prescribes middot": mutate(text, "가운뎃점은 쓰지 않는다.", "명사만 나열할 때는 가운뎃점(·)을 쓴다."),
        "fix uses middot": mutate(text, "열린 작업과 요청 번호, 시계를", "열린 작업·요청 번호·시계를"),
        "rule line uses middot": mutate(text, "Antigravity(안티그래비티)와 Claude", "Antigravity(안티그래비티)·Claude"),
    }
    modes = ["middot"]
else:
    fail("usage: polish_skill_guard.py unpaired|middot")

if passing(text, MODES) != MODES:
    fail("real skill should pass every mode, passes only %s" % passing(text, MODES))
leaks = {name: ok for name, t in mutants.items() if (ok := passing(t, modes))}
if leaks:
    fail("not rejected: " + "; ".join("%s passes %s" % (n, ok) for n, ok in leaks.items()))
print("ok %s guard: %d broken skills rejected by every mode" % (mode, len(mutants)))
