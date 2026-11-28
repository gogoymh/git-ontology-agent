"""Checks that the CI docs job runs the README, demo, banner, intro deck (and its rebuild) and polish skill checks (mission criterion; not a unittest).

Only live `- run:` steps count: a commented-out step (`# - run: ...`) is ignored.

    python3 tests/ci_check.py
"""
import re
import sys
from pathlib import Path

CI = (Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
NEED = ["python tests/readme_check.py", "python tests/readme_links_check.py",
        "python tests/readme_demo_check.py", "python tests/banner_check.py",
        "python tests/banner_check.py size", "python tests/banner_check.py light"] + \
       ["python tests/deck_check.py %s" % m for m in ("valid", "count", "korean", "story")] + \
       ["python tests/deck_build_check.py pinned", "python tests/deck_build_check.py fresh",
        "node ui/scripts/license-check.mjs scripts"] + \
       ["python tests/polish_skill_check.py %s" % m
        for m in ("frontmatter", "start", "list", "particle", "meaning", "middot")] + \
       ["python tests/korean_particle_check.py %s" % m for m in ("rules", "mutations")] + \
       ["python tests/polish_skill_guard.py %s" % m for m in ("unpaired", "middot")]


def docs_runs(text):
    """Commands of uncommented `- run:` steps inside the `docs:` job."""
    runs, in_docs = set(), False
    for line in text.splitlines():
        if re.match(r"^  \S[^:]*:\s*$", line):          # a job key at two-space indent
            in_docs = line.strip() == "docs:"
            continue
        m = re.match(r"^\s*- run:\s*(.+?)\s*$", line)   # no '#' allowed before '- run:'
        if in_docs and m:
            runs.add(m.group(1))
    return runs


missing = [c for c in NEED if c not in docs_runs(CI)]
if missing:
    sys.exit("ci_check: docs job does not run: %s" % ", ".join(missing))
print("ok ci: %d doc checks wired" % len(NEED))
