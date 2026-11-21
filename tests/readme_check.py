"""Checks that README.md carries the spec's explanation (used as a mission criterion; not a unittest module).

    python3 tests/readme_check.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")

# Sections of the spec (Git Ontology Agent.html), in its order.
SECTIONS = ["What it is", "Timeline", "What the ontology does", "How it works", "Relation judgment",
            "Answer rules", "Merging and conflicts", "Commit convention", "Examples", "Out of scope"]
SUBSECTIONS = ["the dependency graph", "rules by type", "wording by status", "Without an ontology"]
PHRASES = ["Interim ①", "Interim ②", "merge commit", "depends_on", "Base", "k1", "k2", "k3", "k4", "k5",
           "confidence", "noul", "Answer: interim", "Answer: final", "cancelled/req/1", "Daegu"]


def main():
    heads = re.findall(r"^#{2,3} (.+)$", README, re.M)
    order = []
    for s in SECTIONS:
        hit = [i for i, h in enumerate(heads) if h.startswith(s)]
        assert hit, "missing section: %s" % s
        order.append(hit[0])
    assert order == sorted(order), "sections out of the spec's order"
    for s in SUBSECTIONS:
        assert any(s in h for h in heads), "missing subsection: %s" % s
    for p in PHRASES:
        assert p in README, "missing: %s" % p
    diagrams = re.findall(r"```mermaid\n(\w+)", README)
    assert "gitGraph" in diagrams and "flowchart" in diagrams, "want a gitGraph timeline and a dependency flowchart"
    assert "docs/assets/banner.png" in README
    code = prose = 0
    fence = None
    for line in README.splitlines():
        if line.startswith("```"):
            fence = None if fence is not None else line[3:].strip()
        elif fence is None:
            prose += bool(line.strip())
        elif fence != "mermaid":
            code += 1
    assert prose > 2 * code, "explanation should outweigh code samples (%d prose vs %d code lines)" % (prose, code)
    print("ok readme: %d sections, %d diagrams, %d prose vs %d code lines" % (len(heads), len(diagrams), prose, code))


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        sys.exit("readme_check: %s" % e)
