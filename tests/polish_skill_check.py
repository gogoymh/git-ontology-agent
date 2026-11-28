"""Checks the polish skill's patterns and fixed examples (mission criterion; not a unittest).

    python3 tests/polish_skill_check.py frontmatter|start|list|particle|meaning|middot [SKILL.md]

Every mode fails when a '- 전:' line lacks its '- 후:' line (or the reverse) or either side is empty.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from korean_particle import attached, mismatches  # noqa: E402

PATH = Path(sys.argv[2]) if len(sys.argv) > 2 else \
    Path(__file__).resolve().parent.parent / ".claude" / "skills" / "polish" / "SKILL.md"
STOCK = re.compile(r"가장 명확|가장 시급|완결된 작업|명확한 범위")
PARTICLE = re.compile(r"을\(를\)|를\(을\)|이\(가\)|가\(이\)|은\(는\)|는\(은\)|와\(과\)|과\(와\)|\(으\)로")
MIDDOT = re.compile("[\u00b7\u318d\u30fb\u2027\u2219]")  # middle dot and look-alikes
TOKEN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_/*.\-]*")


def fail(msg):
    sys.exit("polish_skill_check: " + msg)


def sections(text):
    """Map '## N.' section number to its body."""
    out, cur = {}, None
    for line in text.splitlines():
        m = re.match(r"## (\d+)\.", line)
        if m:
            cur = m.group(1)
            out[cur] = []
        elif line.startswith("## "):
            cur = None
        elif cur:
            out[cur].append(line)
    return {k: "\n".join(v) for k, v in out.items()}


def pairs(body):
    """(before, after) for each '- 전:' line followed by a '- 후:' line; anything else fails."""
    out, before = [], None
    for line in (l.strip() for l in body.splitlines()):
        if not line.startswith(("- 전:", "- 후:")):
            continue
        tag, rest = line[2], line[4:].strip()
        if not rest:
            fail("empty example: " + line)
        if tag == "전":
            if before is not None:
                fail("unpaired example (no '- 후:'): " + before[:40])
            before = rest
        else:
            if before is None:
                fail("unpaired example (no '- 전:'): " + rest[:40])
            out.append((before, rest))
            before = None
    if before is not None:
        fail("unpaired example (no '- 후:'): " + before[:40])
    return out


def sentences(s):
    return [x for x in re.split(r"(?<=[.!?])\s+", s) if x.strip()]


if not PATH.exists():
    fail("missing " + str(PATH))
text = PATH.read_text(encoding="utf-8")
mode = sys.argv[1] if len(sys.argv) > 1 else ""
secs = sections(text)
allpairs = [p for body in secs.values() for p in pairs(body)]

if mode == "frontmatter":
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if not m or not re.search(r"^name: polish$", m.group(1), re.M) or not re.search(r"^description: \S.{40,}", m.group(1), re.M):
        fail("frontmatter needs 'name: polish' and a description")
    print("ok frontmatter")
elif mode == "start":
    ps = pairs(secs.get("1", ""))
    if len(ps) < 3:
        fail("start section has %d examples, need 3" % len(ps))
    for a, b in ps:
        if not STOCK.search(a):
            fail("start example lacks a stock phrase: " + a[:40])
        if STOCK.search(b):
            fail("start fix keeps a stock phrase: " + b[:40])
    print("ok start: %d examples" % len(ps))
elif mode == "list":
    ps = pairs(secs.get("2", ""))
    if len(ps) < 2:
        fail("list section has %d examples, need 2" % len(ps))
    for a, b in ps:
        if max(s.count(",") for s in sentences(a)) < 2:
            fail("list example is not a comma list: " + a[:40])
        bad = [s for s in sentences(b) if s.count(",") > 1]
        if bad:
            fail("list fix sentence has >1 comma: " + bad[0][:40])
        if len(sentences(b)) <= len(sentences(a)):
            fail("list fix was not split: " + b[:40])
    print("ok list: %d examples" % len(ps))
elif mode == "particle":
    ps = pairs(secs.get("3", ""))
    if len([a for a, _ in ps if PARTICLE.search(a)]) < 3:
        fail("particle section needs 3 examples with unresolved particles")
    left = [b for _, b in allpairs if PARTICLE.search(b)]
    if left:
        fail("fix keeps an unresolved particle: " + left[0][:40])
    wrong = [(b, m) for _, b in allpairs for m in mismatches(b)]
    if wrong:
        b, (token, got, want) = wrong[0]
        fail("particle after %s should be %s, not %s: %s" % (token, want, got, b[:40]))
    n = sum(len(attached(b)) for _, b in allpairs)
    print("ok particle: %d examples, %d fixes clean, %d attached particles match" % (len(ps), len(allpairs), n))
elif mode == "meaning":
    if len(allpairs) < 8:
        fail("only %d examples" % len(allpairs))
    for a, b in allpairs:
        lost = set(TOKEN.findall(a)) - set(TOKEN.findall(b))
        if lost:
            fail("fix drops %s: %s" % (sorted(lost), b[:40]))
    print("ok meaning: %d fixes keep names, ids, paths and commands" % len(allpairs))
elif mode == "middot":
    # The app's writing rules forbid the middle dot; quoted originals ('- 전:') may keep it.
    if not re.search(r"가운뎃점\S*\s*쓰지 않는다", text):
        fail("skill does not say to avoid the middle dot")
    for i, line in enumerate(text.splitlines(), 1):
        if MIDDOT.search(line) and not line.strip().startswith("- 전:"):
            fail("middle dot at SKILL.md:%d: %s" % (i, line.strip()[:40]))
    print("ok middot: rules and %d fixes use none" % len(allpairs))
else:
    fail("usage: polish_skill_check.py frontmatter|start|list|particle|meaning|middot [SKILL.md]")
