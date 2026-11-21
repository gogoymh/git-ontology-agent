"""Build the README banner: codex-generated art on the left, title, tagline and pills on the right.

    python scripts/make_banner.py                    # compose every candidate from saved art
    python scripts/make_banner.py --regen            # ask `codex exec` for new art first (all concepts, in parallel)
    python scripts/make_banner.py --regen merge      # only one concept
    python scripts/make_banner.py --pick merge       # also write that candidate, full size, to docs/assets/banner.png

Art is kept in docs/assets/art/<concept>.jpg, composed candidates in docs/assets/candidates/<concept>.png.
Dev-only tool: needs Pillow and network access to fetch Pretendard (SIL OFL 1.1).
The package itself stays dependency-free.
"""
import argparse
import io
import logging
import os
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger("make_banner")

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "docs/assets"
ART_DIR = ASSETS / "art"
CAND_DIR = ASSETS / "candidates"
OUT = ASSETS / "banner.png"
PICK = "layers"

TITLE = "Git Ontology Agent"
TAGLINE = "Every request is a branch. Every answer is a merge."
PILLS = ["Git branches", "Typed claims", "Interim answers"]
PILLS_TAIL = "with any LLM"

FONT_URL = "https://github.com/orioncactus/pretendard/releases/download/v1.3.9/Pretendard-1.3.9.zip"
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "goa-banner"

BASE = """Use your image generation tool to create ONE image, then save the PNG file as art.png in the current directory. Do not create or edit any other file.

Image: a wide landscape 3:2 hero illustration for a README banner, in the calm premium style of an Apple product page.
Background: soft off-white to very light cool grey (#f2f2f6 to #fafafa), seamless studio backdrop, gentle radial light, soft contact shadows. No dark areas in the background.
Composition: the object sits in the LEFT 40% of the frame, centered vertically, fully inside the frame. The RIGHT 60% is completely empty background only, reserved for text added later. No text, no letters, no numbers, no logos, no watermark anywhere.
Style: photorealistic 3D render, matte white ceramic and brushed aluminum, one accent material of translucent violet glass with a soft inner glow (#7c5cff), high detail, minimal, generous negative space.
Object: """

# Each concept fuses the three ideas: git (branch and merge), ontology (typed nodes and links), agent (the glowing core).
CONCEPTS = {
    "merge": "a git branch-and-merge graph built like a molecular model. A vertical trunk of white spheres joined by thin "
             "polished aluminum rods; one side branch forks off to the right, carries two smaller spheres, and merges back "
             "into the trunk. Around the branch, a few small white spheres hang off thin rods like a little knowledge graph. "
             "The sphere where the branch merges back is translucent violet glass with a soft inner glow: the agent.",
    "lanes": "three parallel curved lanes of a git commit graph, made of thin brushed-aluminum rails, seen in gentle "
             "perspective. Lanes fork apart and merge back together. On the rails sit commit nodes of different white "
             "ceramic shapes (spheres, cubes, small cylinders) standing for typed claims, with fine taut threads linking "
             "related nodes across lanes. One translucent violet glass orb glides along the middle lane: the agent.",
    "tree": "a small sculptural tree whose trunk splits into branches like a git graph and rejoins at the top in a smooth "
            "loop. Instead of leaves, the branches end in white ceramic spheres connected to each other by thin aluminum "
            "rods, forming a knowledge graph canopy. At the heart of the canopy sits a translucent violet glass sphere "
            "with a soft inner glow: the agent.",
    "layers": "three thin floating frosted-glass plates stacked with space between them, each a commit snapshot. On each "
              "plate stands a small graph of white ceramic spheres joined by aluminum rods; the graphs differ slightly from "
              "plate to plate. Thin vertical aluminum rods connect matching nodes between plates, and two plates are joined "
              "by a fork-and-merge rod. On the top plate, one sphere is translucent violet glass with a soft glow: the agent.",
    "fork": "the classic git fork icon (two circles joined by a line, with a third circle connected by a curved branch) "
            "rendered as a chunky sculptural object in matte white ceramic with brushed aluminum connectors. Tiny white "
            "spheres sprout from it on thin rods, forming a small ontology graph around the icon. The circle at the end "
            "of the curved branch is translucent violet glass with a soft inner glow: the agent.",
}

# Output geometry: art is scaled to HEIGHT rows, the canvas widened to WIDTH (about 7:3, like auto-ontology-mcp).
WIDTH, HEIGHT, TEXT_X = 1915, 821, 860
PREVIEW = 1200  # candidate width: small enough to keep in the repo


def regen(name):
    work = Path(tempfile.mkdtemp(prefix="goa-art-%s-" % name))
    log.info("generating %s with codex exec in %s", name, work)
    subprocess.run(["codex", "exec", "-s", "workspace-write", "--skip-git-repo-check", BASE + CONCEPTS[name]],
                   cwd=work, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, check=True)
    pngs = sorted(work.rglob("*.png"), key=lambda p: p.stat().st_size)
    if not pngs:
        raise SystemExit("codex did not write an image for %s (%s)" % (name, work))
    ART_DIR.mkdir(parents=True, exist_ok=True)
    from PIL import Image

    Image.open(pngs[-1]).convert("RGB").save(ART_DIR / ("%s.jpg" % name), quality=92)
    shutil.rmtree(work, ignore_errors=True)
    log.info("saved art %s", name)


def font(weight):
    name = "Pretendard-%s.otf" % weight
    path = CACHE / name
    if not path.exists():
        log.info("downloading Pretendard from %s", FONT_URL)
        data = urllib.request.urlopen(FONT_URL, timeout=60).read()
        CACHE.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for member in z.namelist():
                if "/static/" in member and member.endswith(".otf") and "/alternative/" not in member:
                    (CACHE / Path(member).name).write_bytes(z.read(member))
    return str(path)


def crop_art(art):
    """Crop the art to its object (plus a margin of its own background) and scale it to fill the left panel."""
    from PIL import Image, ImageChops, ImageFilter

    bg = Image.new("RGB", art.size, art.getpixel((art.width - 4, art.height // 2)))
    diff = ImageChops.difference(art, bg).convert("L").point(lambda v: 255 if v > 22 else 0)
    x0, y0, x1, y1 = diff.filter(ImageFilter.MinFilter(5)).getbbox()  # MinFilter drops speckle noise
    pad = 90
    obj = art.crop((max(x0 - pad, 0), max(y0 - pad, 0), min(x1 + pad, art.width), min(y1 + pad, art.height)))
    scale = min(HEIGHT * 0.92 / obj.height, (TEXT_X - 120) / obj.width)
    return obj.resize((int(obj.width * scale), int(obj.height * scale)), Image.LANCZOS)


def place_art(obj, backdrop, pad=70):
    from PIL import Image, ImageFilter

    mask = Image.new("L", obj.size, 0)
    inner = Image.new("L", (obj.width - 2 * pad, obj.height - 2 * pad), 255)
    mask.paste(inner, (pad, pad))
    mask = mask.filter(ImageFilter.GaussianBlur(pad // 2))
    x = (TEXT_X - 60 - obj.width) // 2 + 20
    backdrop.paste(obj, (x, (HEIGHT - obj.height) // 2), mask)


def compose(name):
    from PIL import Image, ImageDraw, ImageFont

    obj = crop_art(Image.open(ART_DIR / ("%s.jpg" % name)).convert("RGB"))
    # Backdrop: a vertical gradient between the cropped art's own top and bottom tones, so the seam disappears.
    row = lambda y: obj.crop((0, y, obj.width, y + 4)).resize((1, 1), Image.BOX).getpixel((0, 0))
    top, bottom = row(0), row(obj.height - 4)
    canvas = Image.new("RGB", (WIDTH, HEIGHT))
    grad = Image.linear_gradient("L").resize((WIDTH, HEIGHT))
    canvas.paste(Image.composite(Image.new("RGB", (WIDTH, HEIGHT), bottom), Image.new("RGB", (WIDTH, HEIGHT), top), grad))
    place_art(obj, canvas)

    draw = ImageDraw.Draw(canvas)
    title, tag, pill = ImageFont.truetype(font("Bold"), 104), ImageFont.truetype(font("Regular"), 38), \
        ImageFont.truetype(font("Medium"), 30)
    y = 268
    draw.text((TEXT_X, y), TITLE, font=title, fill=(29, 29, 31), anchor="lt")
    y += 104 + 42
    draw.text((TEXT_X, y), TAGLINE, font=tag, fill=(110, 110, 115), anchor="lt")
    y += 38 + 56
    x = TEXT_X
    for text in PILLS:
        w = draw.textlength(text, font=pill)
        box = (x, y, x + w + 52, y + 56)
        draw.rounded_rectangle((box[0], box[1] + 2, box[2], box[3] + 2), 28, fill=(226, 226, 232))  # soft drop
        draw.rounded_rectangle(box, 28, fill=(255, 255, 255), outline=(226, 226, 232), width=2)
        draw.text((x + 26, y + 28), text, font=pill, fill=(29, 29, 31), anchor="lm")
        x = box[2] + 16
    draw.text((x + 8, y + 28), PILLS_TAIL, font=pill, fill=(134, 134, 139), anchor="lm")
    return canvas


def save(name, pick):
    from PIL import Image

    canvas = compose(name)
    CAND_DIR.mkdir(parents=True, exist_ok=True)
    preview = canvas.resize((PREVIEW, PREVIEW * HEIGHT // WIDTH), Image.LANCZOS)
    preview.save(CAND_DIR / ("%s.png" % name), optimize=True)
    if name == pick:
        canvas.save(OUT, optimize=True)
        log.info("banner.png <- %s", name)
    log.info("wrote candidate %s", name)


def main():
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("names", nargs="*", help="concepts: %s (default: all)" % ", ".join(CONCEPTS))
    parser.add_argument("--regen", action="store_true", help="generate new art with codex first")
    parser.add_argument("--pick", default=PICK, help="candidate copied to banner.png (default: %(default)s)")
    args = parser.parse_args()
    names = args.names or list(CONCEPTS)
    unknown = set(names) - set(CONCEPTS)
    if unknown:
        parser.error("unknown concept: %s" % ", ".join(sorted(unknown)))
    todo = [n for n in names if args.regen or not (ART_DIR / ("%s.jpg" % n)).exists()]
    with ThreadPoolExecutor(len(todo) or 1) as pool:
        list(pool.map(regen, todo))
    for name in names:
        save(name, args.pick)


if __name__ == "__main__":
    main()
