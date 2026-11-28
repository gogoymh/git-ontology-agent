"""Checks tests/korean_particle.py and the particle check that uses it (mission criterion; not a unittest).

    python3 tests/korean_particle_check.py rules|mutations
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SKILL = ROOT / ".claude" / "skills" / "polish" / "SKILL.md"
sys.path.insert(0, str(HERE))
from korean_particle import mismatches, pick  # noqa: E402

# (word, any form of the pair, expected form): SKILL.md section 3 rules and examples.
CASES = [
    # Hangul final consonant, and 로 after ㄹ
    ("작업", "를", "을"), ("기록", "가", "이"), ("화면", "는", "은"), ("이름", "와", "과"), ("파일", "으로", "로"),
    ("코드", "을", "를"), ("미션", "로", "으로"), ("사과", "은", "는"),
    # Numbers by their reading: 1 일, 3 삼, 6 육, 7 칠, 8 팔, 10 십, 0 영 have a final; 2 이, 4 사, 5 오, 9 구 do not
    ("GOA-14", "을", "를"), ("GOA-17", "을", "을"), ("GOA-17", "으로", "로"), ("GOA-23", "를", "을"),
    ("GOA-11", "으로", "로"), ("GOA-13", "로", "으로"), ("GOA-16", "가", "이"), ("GOA-18", "으로", "로"),
    ("GOA-10", "로", "으로"), ("GOA-20", "를", "을"), ("GOA-100", "는", "은"), ("GOA-0", "와", "과"),
    ("GOA-2", "을", "를"), ("GOA-15", "을", "를"), ("GOA-19", "이", "가"),
    # English names by their reading
    ("Antigravity", "이", "가"), ("Claude", "으로", "로"), ("Claude", "은", "는"), ("README", "이", "가"),
    ("CI", "은", "는"), ("PR", "으로", "로"), ("PPT", "을", "를"), ("UX", "과", "와"),
    ("Slack", "를", "을"), ("main", "를", "을"), ("goal", "으로", "로"), ("chat", "를", "을"),
    ("test", "을", "를"), ("tasks", "을", "를"), ("/connect", "으로", "로"), ("serve", "을", "를"),
]

# Each wrong form must make 'polish_skill_check.py particle' fail.
MUTATIONS = [
    ("- 후: GOA-14를 마치고", "- 후: GOA-14을 마치고"),
    ("GOA-15를 Claude로", "GOA-15를 Claude으로"),
    ("- 후: Antigravity가", "- 후: Antigravity이"),
    ("- 후: GOA-23을 맡았습니다.", "- 후: GOA-23를 맡았습니다."),
    ("- 후: GOA-17을 끝내지", "- 후: GOA-17를 끝내지"),
]


def fail(msg):
    sys.exit("korean_particle_check: " + msg)


def particle_check(text):
    with tempfile.TemporaryDirectory(prefix="pq-particle.") as tmp:
        t = Path(tmp)
        (t / ".claude" / "skills" / "polish").mkdir(parents=True)
        (t / "tests").mkdir()
        for name in ("polish_skill_check.py", "korean_particle.py"):
            shutil.copy(HERE / name, t / "tests" / name)
        (t / ".claude" / "skills" / "polish" / "SKILL.md").write_text(text, encoding="utf-8")
        return subprocess.run([sys.executable, str(t / "tests" / "polish_skill_check.py"), "particle"],
                              capture_output=True, text=True)


mode = sys.argv[1] if len(sys.argv) > 1 else ""
if mode == "rules":
    bad = [(w, p, want, pick(w, p)) for w, p, want in CASES if pick(w, p) != want]
    if bad:
        fail("wrong pick: " + "; ".join("%s+%s gave %s, want %s" % (w, p, got, want) for w, p, want, got in bad))
    if mismatches("4가지로 검사합니다") or mismatches("req/* 요청이"):
        fail("bare numbers and paths without a sound must be skipped")
    print("ok rules: %d cases" % len(CASES))
elif mode == "mutations":
    text = SKILL.read_text(encoding="utf-8")
    r = particle_check(text)
    if r.returncode != 0:
        fail("particle check fails on the real skill: " + (r.stderr or r.stdout).strip())
    for old, new in MUTATIONS:
        if old not in text:
            fail("mutation does not apply: " + old)
        r = particle_check(text.replace(old, new, 1))
        if r.returncode == 0 or "should be" not in r.stderr:
            fail("wrong particle passes: " + new)
    print("ok mutations: %d wrong particles caught" % len(MUTATIONS))
else:
    fail("usage: korean_particle_check.py rules|mutations")
