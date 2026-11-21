"""Stdlib-only checks for docs/assets/banner.png (used as mission criteria; not a unittest module).

    python3 tests/banner_check.py size         # PNG, width >= 2.2 x height
    python3 tests/banner_check.py light        # white-toned backdrop
    python3 tests/banner_check.py layout       # art on the left, dark title block on the right
    python3 tests/banner_check.py candidates   # >= 4 distinct, white-toned candidates
"""
import struct
import sys
import zlib
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "docs/assets"
PNG = ASSETS / "banner.png"


def read_png(path):
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    pos, idat, head = 8, b"", None
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        if kind == b"IHDR":
            head = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        pos += 12 + n
    w, h, depth, color, _, _, interlace = head
    assert depth == 8 and color in (2, 6) and not interlace, "expect 8-bit RGB/RGBA"
    bpp = 3 if color == 2 else 4
    raw, stride, rows, prev = zlib.decompress(idat), w * bpp, [], bytearray(w * bpp)
    for y in range(h):
        f, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b, c = prev[i], prev[i - bpp] if i >= bpp else 0
            if f == 1: line[i] = (line[i] + a) & 255
            elif f == 2: line[i] = (line[i] + b) & 255
            elif f == 3: line[i] = (line[i] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append([sum(line[x * bpp:x * bpp + 3]) // 3 for x in range(w)])
        prev = line
    return w, h, rows


def light(path):
    w, h, rows = read_png(path)
    edge = [v for r in rows[:h // 20] + rows[-h // 20:] for v in r] + [v for r in rows for v in r[-w // 20:]]
    edge.sort()
    median, dark = edge[len(edge) // 2], sum(v < 200 for v in edge) / len(edge)
    assert median >= 225 and dark < 0.01, "%s backdrop is not white-toned (median %d, dark %.3f)" % (path.name, median, dark)
    return w, h, rows, median


def main(mode):
    if mode == "candidates":
        files = sorted((ASSETS / "candidates").glob("*.png"))
        sigs = set()
        for f in files:
            w, h, rows, median = light(f)
            assert w >= 2.2 * h, "%s is %dx%d" % (f.name, w, h)
            sigs.add(tuple(sum(rows[y][x] for y in range(h // 8, h, h // 8)) // 64 for x in range(0, w // 2, w // 32)))
        assert len(files) >= 4 and len(sigs) == len(files), "want >= 4 distinct candidates, got %d" % len(files)
        print("ok candidates %s" % ", ".join(f.stem for f in files))
        return
    w, h, rows, median = light(PNG)
    if mode == "size":
        assert w >= 2.2 * h, "banner is %dx%d, want width >= 2.2 x height" % (w, h)
        print("ok size %dx%d" % (w, h))
    elif mode == "light":
        print("ok light backdrop median %d" % median)
    else:
        ink = lambda x0, x1: sum(abs(v - median) > 40 for r in rows for v in r[x0:x1])
        art = ink(0, int(w * 0.4))
        # Thin optical-glass branches have less contrast than solid artwork.
        assert art > w * h * 0.01, "no art on the left (%d px)" % art
        dark = [y for y in range(h) if sum(v < 80 for v in rows[y][int(w * 0.44):]) > 3]
        assert dark, "no dark title on the right"
        assert dark[0] > h * 0.2 and dark[-1] < h * 0.8 and dark[-1] - dark[0] < h * 0.5, \
            "right side should hold one centered text block (rows %d..%d)" % (dark[0], dark[-1])
        print("ok layout art=%dpx text rows %d..%d" % (art, dark[0], dark[-1]))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "layout")
