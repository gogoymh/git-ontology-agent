"""Checks that README's `goa demo` samples match a real run, commit hashes aside (mission criterion; not a unittest).

    python3 tests/readme_demo_check.py
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
HASH = re.compile(r"@[0-9a-f]{7}\b")


def run(*argv):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GOA_")}
    env.update(GOA_ENV_FILE=os.devnull, NO_COLOR="1", PYTHONPATH=str(ROOT))
    with tempfile.TemporaryDirectory() as d:     # a scratch directory: no .env, no repo
        out = subprocess.run([sys.executable, "-m", "goa", "demo", *argv], cwd=d, env=env,
                             capture_output=True, text=True, timeout=60, check=True).stdout
    lines = out.splitlines()[2:]
    end = next(i for i, l in enumerate(lines) if l.startswith("* "))
    return "\n".join(lines[:end]).strip("\n")


def main():
    samples = re.findall(r"`goa demo (--scenario \w+ --lang \w+)`\n\n```text\n(.*?)\n```", README, re.S)
    assert len(samples) >= 2, "want at least 2 goa demo samples in README"
    for argv, block in samples:
        real = run(*argv.split())
        assert HASH.sub("@#", block) == HASH.sub("@#", real), \
            "README sample for `goa demo %s` differs from a real run:\n%s" % (argv, real)
    assert "goa demo --delay 0" in README, "usage lines should show --delay"
    print("ok readme demo: %d samples match a real run" % len(samples))


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        sys.exit("readme_demo_check: %s" % e)
