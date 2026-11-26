"""Checks that every local link and image in README.md points at a file that exists (mission criterion; not a unittest).

    python3 tests/readme_links_check.py [README.md]
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINK = re.compile(r"\]\(([^)\s]+)[^)]*\)|\b(?:src|href)=\"([^\"]+)\"")


def targets(text):
    text = re.sub(r"^```.*?^```", "", text, flags=re.M | re.S)   # skip code blocks
    for m in LINK.finditer(text):
        t = (m.group(1) or m.group(2)).split("#")[0].split("?")[0]
        if t and not re.match(r"[a-z][a-z0-9+.-]*:", t, re.I):    # local paths only
            yield t


def main(readme):
    base = readme.parent
    found = sorted(set(targets(readme.read_text(encoding="utf-8"))))
    missing = [t for t in found if not (base / t).exists()]
    if missing:
        sys.exit("readme_links_check: missing link targets: %s" % ", ".join(missing))
    print("ok readme links: %d local targets exist" % len(found))


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "README.md")
