"""Checks the Korean intro deck docs/goa-intro-ko.pptx (mission criterion helper; not a unittest module).

    python3 tests/deck_check.py count|korean|story|valid
"""
import re
import sys
import zipfile
from pathlib import Path
from xml.dom import minidom

DECK = Path(__file__).resolve().parent.parent / "docs" / "goa-intro-ko.pptx"


def slides():
    with zipfile.ZipFile(DECK) as z:
        names = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                       key=lambda n: int(re.findall(r"\d+", n)[0]))
        return [z.read(n).decode("utf-8") for n in names]


def text(xml):
    return " ".join(re.findall(r"<a:t>([^<]*)</a:t>", xml))


def check_count():
    n = len(slides())
    assert n == 3, "expected 3 slides, found %d" % n


def check_korean():
    for i, s in enumerate(slides(), 1):
        t = text(s)
        hangul = len(re.findall(r"[가-힣]", t))
        latin = len(re.findall(r"[A-Za-z]", t))
        assert hangul >= 40, "slide %d has too little Korean (%d chars)" % (i, hangul)
        assert hangul > latin, "slide %d is mostly non-Korean (%d hangul vs %d latin)" % (i, hangul, latin)


STORY = [
    ["Git", "온톨로지", "브랜치", "중간 답변", "최종 답변"],          # 1: what it is and why
    ["보완", "정정", "독립", "취소", "진행", "depends_on"],          # 2: relation judgment + redo only dependents
    ["병합", "타입", "중간 답변", "최종 답변", "출처"],               # 3: merge by type, answers from branch/merge
]


def check_story():
    for i, (s, words) in enumerate(zip(slides(), STORY), 1):
        t = text(s)
        missing = [w for w in words if w not in t]
        assert not missing, "slide %d misses %s" % (i, missing)


def check_valid():
    with zipfile.ZipFile(DECK) as z:
        assert z.testzip() is None, "corrupt zip member"
        names = z.namelist()
        for need in ["[Content_Types].xml", "ppt/presentation.xml", "ppt/_rels/presentation.xml.rels"]:
            assert need in names, "missing part " + need
        for n in names:
            if n.endswith(".xml") or n.endswith(".rels"):
                minidom.parseString(z.read(n))
        pres = z.read("ppt/presentation.xml").decode("utf-8")
        assert len(re.findall(r"<p:sldId ", pres)) == 3, "presentation.xml does not list 3 slides"


if __name__ == "__main__":
    {"count": check_count, "korean": check_korean, "story": check_story, "valid": check_valid}[sys.argv[1]]()
    print("ok", sys.argv[1])
