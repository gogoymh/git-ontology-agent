"""Stdlib-only checks for docs/assets/banner.png (used as mission criteria; not a unittest module).

    python3 tests/banner_check.py size     # PNG, width >= 2.5 x height
    python3 tests/banner_check.py layout   # symbol on the left, only a title line on the right
"""
import struct
import sys
import zlib
from pathlib import Path

PNG = Path(__file__).resolve().parent.parent / "docs/assets/banner.png"


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


def main(mode):
    w, h, rows = read_png(PNG)
    if mode == "size":
        assert w >= 2.5 * h, "banner is %dx%d, want width >= 2.5 x height" % (w, h)
        print("ok size %dx%d" % (w, h))
        return
    bright = lambda x0, x1, y0=0, y1=h: sum(v > 140 for r in rows[y0:y1] for v in r[x0:x1])
    symbol = bright(0, w // 4)
    assert symbol > w * h * 0.005, "no symbol on the left (%d bright px)" % symbol
    gap = bright(w // 4, int(w * 0.36))
    assert gap == 0, "left symbol spills toward the right (%d bright px)" % gap
    lit = [y for y in range(h) if any(v > 140 for v in rows[y][int(w * 0.36):])]
    assert lit, "no title on the right"
    band = lit[-1] - lit[0]
    assert band < h * 0.3 and lit[0] > h * 0.25 and lit[-1] < h * 0.75, \
        "right side should hold one centered title line (rows %d..%d)" % (lit[0], lit[-1])
    print("ok layout symbol=%dpx title rows %d..%d" % (symbol, lit[0], lit[-1]))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "layout")
