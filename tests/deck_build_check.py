"""Checks that the Korean intro deck rebuilds from a fresh clone (mission criterion helper; not a unittest).

    python3 tests/deck_build_check.py pinned   # scripts/package.json pins pptxgenjs (MIT) and the lockfile agrees
    python3 tests/deck_build_check.py fresh    # README's one-liner builds a deck that passes tests/deck_check.py

`fresh` copies the tracked files into a temp folder (no node_modules), runs the README command there
and needs node, npm and the npm registry or cache.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CMD = "npm --prefix scripts ci && node scripts/make_deck_ko.js"


def check_pinned():
    pkg = json.loads((ROOT / "scripts/package.json").read_text(encoding="utf-8"))
    want = pkg.get("dependencies", {}).get("pptxgenjs", "")
    assert re.fullmatch(r"\d+\.\d+\.\d+", want), "pptxgenjs is not pinned to one exact version: %r" % want
    lock = json.loads((ROOT / "scripts/package-lock.json").read_text(encoding="utf-8"))
    got = lock["packages"].get("node_modules/pptxgenjs", {})
    assert got.get("version") == want, "lockfile has pptxgenjs %s, package.json %s" % (got.get("version"), want)
    assert got.get("license") == "MIT", "pptxgenjs license is %s, not MIT" % got.get("license")
    assert lock["packages"][""]["dependencies"].get("pptxgenjs") == want, "lockfile root is out of date"
    print("ok pinned: pptxgenjs %s (MIT)" % want)


def check_fresh():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "`%s`" % CMD in readme, "README does not give the one-line rebuild command"
    d = Path(tempfile.mkdtemp(prefix="goa-deck-"))
    try:
        files = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "scripts", "docs/assets/banner.png"],
                               capture_output=True, text=True, check=True).stdout.split("\0")
        for f in filter(None, files):
            (d / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / f, d / f)
        for f in ("scripts/package.json", "scripts/package-lock.json"):
            assert (d / f).exists(), "%s is not tracked by git" % f
        r = subprocess.run(CMD, shell=True, cwd=d, capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, "README command failed:\n%s%s" % (r.stdout[-1500:], r.stderr[-1500:])
        deck = d / "docs/goa-intro-ko.pptx"
        assert deck.exists(), "README command did not write docs/goa-intro-ko.pptx"
        env = dict(os.environ, GOA_DECK=str(deck))
        for m in ("valid", "count", "korean", "story"):
            c = subprocess.run([sys.executable, str(ROOT / "tests/deck_check.py"), m], env=env, capture_output=True, text=True)
            assert c.returncode == 0, (c.stdout + c.stderr).strip()
    finally:
        shutil.rmtree(d, True)
    print("ok fresh: README command built a deck that passes 4 deck checks")


if __name__ == "__main__":
    try:
        {"pinned": check_pinned, "fresh": check_fresh}[sys.argv[1]]()
    except (AssertionError, subprocess.SubprocessError, OSError, KeyError, ValueError) as e:
        sys.exit("deck_build_check %s: %s" % (sys.argv[1], e))
