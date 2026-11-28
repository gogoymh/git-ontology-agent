"""Reproduces the gaps found while grading GOA-23 (mission criterion; not a unittest).

Each mode exits 0 when the gap is still there, so the follow-up ticket's evidence stays checkable.

    python3 tests/polish_gaps_check.py dropped-fix|ci|middot
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / ".claude" / "skills" / "polish" / "SKILL.md"
CHECK = ROOT / "tests" / "polish_skill_check.py"
HELPER = ROOT / "tests" / "korean_particle.py"


def fail(msg):
    sys.exit("polish_gaps_check: " + msg)


def run_mutated(mutate, modes):
    """Copy the skill and its check into a temp tree, mutate the skill, return modes that pass."""
    with tempfile.TemporaryDirectory(prefix="pq-polish-gap.") as tmp:
        t = Path(tmp)
        (t / ".claude" / "skills" / "polish").mkdir(parents=True)
        (t / "tests").mkdir()
        shutil.copy(CHECK, t / "tests" / CHECK.name)
        shutil.copy(HELPER, t / "tests" / HELPER.name)
        text = SKILL.read_text(encoding="utf-8")
        new = mutate(text)
        if new == text:
            fail("mutation did not apply")
        (t / ".claude" / "skills" / "polish" / "SKILL.md").write_text(new, encoding="utf-8")
        ok = []
        for m in modes:
            r = subprocess.run([sys.executable, str(t / "tests" / CHECK.name), m], capture_output=True)
            if r.returncode == 0:
                ok.append(m)
        return ok


mode = sys.argv[1] if len(sys.argv) > 1 else ""
ALL = ["frontmatter", "start", "list", "particle", "meaning"]

if mode == "dropped-fix":
    # Remove the last '- 후:' line (SKILL.md:69); zip() in pairs() drops the orphan '- 전:'.
    ok = run_mutated(lambda s: s.replace("- 후: GOA-23을 맡았습니다.\n", "", 1), ALL)
    if ok != ALL:
        fail("gap closed: an orphan example now fails " + str(sorted(set(ALL) - set(ok))))
    print("gap: an example without its fix passes all five checks")
elif mode == "ci":
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    if "polish_skill_check" in ci:
        fail("gap closed: CI runs polish_skill_check")
    print("gap: CI does not run polish_skill_check.py")
elif mode == "middot":
    lines = SKILL.read_text(encoding="utf-8").splitlines()
    rule = [i + 1 for i, l in enumerate(lines) if "가운뎃점(·)을 쓴다" in l]
    fixes = [i + 1 for i, l in enumerate(lines) if l.startswith("- 후:") and "·" in l]
    if not rule or not fixes:
        fail("gap closed: skill no longer prescribes the middle dot")
    print("gap: SKILL.md:%d prescribes '·' and fixes at lines %s use it" % (rule[0], fixes))
else:
    fail("usage: polish_gaps_check.py dropped-fix|ci|middot")
