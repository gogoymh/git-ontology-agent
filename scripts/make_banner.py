"""Build docs/assets/banner.png: a codex-generated symbol on the left, the title in Pretendard on the right.

    python scripts/make_banner.py            # compose from docs/assets/banner-art.png
    python scripts/make_banner.py --regen    # first ask `codex exec` for new art

Dev-only tool: needs Pillow and network access to fetch Pretendard (SIL OFL 1.1).
The package itself stays dependency-free.
"""
import argparse
import io
import logging
import os
import subprocess
import urllib.request
import zipfile
from pathlib import Path

log = logging.getLogger("make_banner")

ROOT = Path(__file__).resolve().parent.parent
ART = ROOT / "docs/assets/banner-art.png"
OUT = ROOT / "docs/assets/banner.png"
TITLE = "Git Ontology Agent"
FONT_URL = "https://github.com/orioncactus/pretendard/releases/download/v1.3.9/Pretendard-1.3.9.zip"
FONT_NAME = "Pretendard-SemiBold.otf"
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "goa-banner"

# Apple-keynote style prompt: symbol in the left third, right side left empty for the title.
PROMPT = """Use your image generation tool to create ONE image, then save the PNG file to docs/assets/banner-art.png in the current directory (create folders if needed). Do not edit any other file.

Image: a wide landscape 3:2 hero banner, designed as if by Apple's industrial design team for a keynote slide.
Composition: the symbol sits in the LEFT THIRD of the frame, centered vertically. The RIGHT TWO THIRDS are completely empty, smooth background only, reserved for a title added later. No text, no letters, no logos, no watermark anywhere.
Symbol: a single elegant glass-and-aluminum object that fuses a git branch graph with an ontology: three softly glowing translucent glass spheres (nodes) connected by smooth polished curved tubes that fork from one trunk and merge back into one, forming a clean branch-and-merge shape. Subtle inner light in cool blue to violet hues, frosted glass, precise chamfered edges, gentle refraction.
Style: minimal, premium, calm. Deep near-black navy background (#0b1020) with a very soft radial glow behind the symbol, studio lighting, soft reflection on an invisible floor, high detail, photorealistic 3D render, generous negative space.
"""

# Output geometry, in source-art pixels: keep rows TOP..TOP+HEIGHT, widen to WIDTH (8:3).
TOP, HEIGHT, WIDTH = 110, 900, 2400


def regen_art():
    log.info("generating art with codex exec")
    subprocess.run(["codex", "exec", "-s", "workspace-write", "--skip-git-repo-check", PROMPT],
                   cwd=ROOT, stdin=subprocess.DEVNULL, check=True)
    if not ART.exists():
        raise SystemExit("codex did not write %s" % ART)


def font_path():
    path = CACHE / FONT_NAME
    if not path.exists():
        log.info("downloading Pretendard from %s", FONT_URL)
        data = urllib.request.urlopen(FONT_URL, timeout=60).read()
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            member = next(n for n in z.namelist() if n.endswith("static/" + FONT_NAME))
            CACHE.mkdir(parents=True, exist_ok=True)
            path.write_bytes(z.read(member))
    return path


def compose():
    from PIL import Image, ImageDraw, ImageFont

    art = Image.open(ART).convert("RGB")
    art = art.crop((0, TOP, art.width, TOP + HEIGHT))
    # Widen the canvas by repeating each row's right-edge color: the background is flat there.
    edge = art.crop((art.width - 1, 0, art.width, HEIGHT)).resize((WIDTH - art.width, HEIGHT))
    canvas = Image.new("RGB", (WIDTH, HEIGHT))
    canvas.paste(art, (0, 0))
    canvas.paste(edge, (art.width, 0))

    font = ImageFont.truetype(str(font_path()), 150)
    draw = ImageDraw.Draw(canvas)
    left, top, right, bottom = draw.textbbox((0, 0), TITLE, font=font)
    x = 760 + (WIDTH - 760 - (right - left)) // 2 - left
    y = (HEIGHT - (bottom - top)) // 2 - top
    draw.text((x, y), TITLE, font=font, fill=(245, 245, 247))

    canvas = canvas.resize((WIDTH * 2 // 3, HEIGHT * 2 // 3), Image.LANCZOS)
    canvas.save(OUT, optimize=True)
    log.info("wrote %s (%dx%d)", OUT, *canvas.size)


def main():
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--regen", action="store_true", help="generate new art with codex first")
    args = parser.parse_args()
    if args.regen or not ART.exists():
        regen_art()
    compose()


if __name__ == "__main__":
    main()
