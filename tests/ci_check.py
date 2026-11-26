"""Checks that the CI workflow runs the README and intro deck checks (mission criterion; not a unittest).

    python3 tests/ci_check.py
"""
import sys
from pathlib import Path

CI = (Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
NEED = ["python tests/readme_check.py", "python tests/readme_links_check.py"] + \
       ["python tests/deck_check.py %s" % m for m in ("valid", "count", "korean", "story")]

missing = [c for c in NEED if c not in CI]
if missing:
    sys.exit("ci_check: workflow does not run: %s" % ", ".join(missing))
print("ok ci: %d doc checks wired" % len(NEED))
