#!/usr/bin/env python3
"""GOA-42: check that the grading documents sit under one parent document.

Usage: python3 tests/goa42_docs_check.py tree|index|kept|overlap
Reads the project's Preconquer notes ($PRECONQUER_HOME or ~/.preconquer).
Exits 0 only when the named criterion holds.
"""
import glob
import hashlib
import json
import os
import re
import sys

PARENT = "미션 채점 기록"
# Each grading document with the sha256 its text had before GOA-42: moving must not change a word.
CHILDREN = {
    "GOA-28과 GOA-35 채점": "a5903c2f00a8d63a66bf007b1072597e6a9a320973590747576f399beadd8a04",
    "GOA-18 채점": "ba73d7d96fee199c335e371f5d683bfa5925f9c70f7113eda65e272bdad7034e",
    "GOA-22 채점 다섯 미션": "e25598522e9c4cd0cc3df901e83feeb385297dc11a2b04af34c0777248b55eae",
    "GOA-23 채점- 76점": "fceab55e151734cc7b839a365e0a5fc73dd107e44779e587ae0a12d2e1c29652",
}
OTHER = {"GOA 성능 벤치마크 가이드와 초기 기준 점수": "861a1c3071eb8853afdd6a7a770502c8acdb0b9edd17a7ef6902439d2a1802cd"}
# One index line per grading document: its link, then every ticket and score it grades.
INDEX = {
    "GOA-28과 GOA-35 채점": [("GOA-28", "84"), ("GOA-35", "40")],
    "GOA-18 채점": [("GOA-18", "70")],
    "GOA-22 채점 다섯 미션": [("GOA-14", "85"), ("GOA-15", "76"), ("GOA-17", "86"), ("GOA-11", "82"), ("GOA-20", "84")],
    "GOA-23 채점- 76점": [("GOA-23", "76")],
}
# Facts more than one grading document reports; the parent must bring each together.
OVERLAP = [
    ["GOA-15", "GOA-28", "79.6", "3.0", "GOA-38"],
    ["GOA-30", "315.471", "noisy-baseline"],
    ["ci.yml:24-29", "GOA-20", "GOA-23", "GOA-26", "GOA-21"],
    ["GOA-36", "ci.yml:14"],
]


def docs_dir():
    home = os.environ.get("PRECONQUER_HOME") or os.path.expanduser("~/.preconquer")
    found = glob.glob(os.path.join(home, "hub", "git-ontology-agent-*", "docs"))
    if not found:
        sys.exit("no git-ontology-agent notes under " + home)
    return found[0]


def read(folder, name):
    path = os.path.join(folder, name + ".md")
    if not os.path.exists(path):
        sys.exit("missing document: " + name)
    with open(path, "rb") as f:
        return f.read()


def parents(folder):
    path = os.path.join(folder, ".tree.json")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f).get("parents", {})


def check_tree(folder):
    tree = parents(folder)
    read(folder, PARENT)
    if tree.get(PARENT):
        sys.exit(PARENT + " is not at the top")
    for name in CHILDREN:
        if tree.get(name) != PARENT:
            sys.exit(f"{name} is under {tree.get(name)!r}, not {PARENT}")
    print("all four grading documents are under", PARENT)


def check_index(folder):
    lines = read(folder, PARENT).decode().splitlines()
    for name, pairs in INDEX.items():
        hits = [l for l in lines if f"[[{name}]]" in l]
        if len(hits) != 1:
            sys.exit(f"want one index line linking [[{name}]], found {len(hits)}")
        for ticket, score in pairs:
            if not re.search(re.escape(ticket) + r"(?!\d)[^|]*?(?<![\d.])" + re.escape(score) + r"(?![\d.])", hits[0]):
                sys.exit(f"index line for {name} lacks {ticket} with {score}")
    print("index has one line per grading document with every ticket and score")


def check_kept(folder):
    for name, sha in {**CHILDREN, **OTHER}.items():
        if hashlib.sha256(read(folder, name)).hexdigest() != sha:
            sys.exit(f"{name} text changed")
    print("every document is kept with its text unchanged")


def check_overlap(folder):
    text = read(folder, PARENT).decode()
    if "## 겹치는 내용" not in text:
        sys.exit("parent has no 겹치는 내용 section")
    part = text.split("## 겹치는 내용", 1)[1].split("\n## ", 1)[0]
    for group in OVERLAP:
        bullets = [l for l in part.splitlines() if all(word in l for word in group)]
        if not bullets:
            sys.exit("no overlap line with all of " + ", ".join(group))
    print("every shared fact is merged into one line of the parent")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    checks = {"tree": check_tree, "index": check_index, "kept": check_kept, "overlap": check_overlap}
    if mode not in checks:
        sys.exit(__doc__)
    checks[mode](docs_dir())
