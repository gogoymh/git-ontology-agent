"""Mutation checks for tests/ci_check.py (mission criterion; not a unittest).

    python3 tests/ci_check_guard.py   # exits 0 when every broken workflow is rejected
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CI = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")


def run(ci):
    d = Path(tempfile.mkdtemp(prefix="goa-ciguard-"))
    try:
        (d / "tests").mkdir()
        (d / ".github/workflows").mkdir(parents=True)
        shutil.copy(ROOT / "tests/ci_check.py", d / "tests/ci_check.py")
        (d / ".github/workflows/ci.yml").write_text(ci, encoding="utf-8")
        return subprocess.run([sys.executable, str(d / "tests/ci_check.py")], capture_output=True, text=True).returncode
    finally:
        shutil.rmtree(d, True)


MUTANTS = {
    "demo step commented": re.sub(r"^(\s*)- run: (python tests/readme_demo_check\.py)", r"\1# - run: \2", CI, flags=re.M),
    "banner step commented": re.sub(r"^(\s*)- run: (python tests/banner_check\.py)$", r"\1#- run: \2", CI, flags=re.M),
    "banner step removed": re.sub(r"^\s*- run: python tests/banner_check\.py\n", "", CI, flags=re.M),
    "demo step moved to ui job": CI.replace("      - run: python tests/readme_demo_check.py\n", "", 1)
                                   + "      - run: python tests/readme_demo_check.py\n",
}
assert run(CI) == 0, "real workflow should pass"
bad = [name for name, ci in MUTANTS.items() if ci == CI or run(ci) == 0]
if bad:
    sys.exit("ci_check_guard: not rejected: %s" % ", ".join(bad))
print("ok ci guard: %d broken workflows rejected" % len(MUTANTS))
