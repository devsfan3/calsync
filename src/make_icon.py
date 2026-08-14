#!/usr/bin/env python3
"""Draw CalSync's app icon as a 1024px PNG, with no third-party libraries.

Shapes are rendered from signed distance functions so the edges come out smooth
without supersampling — a rounded rectangle's distance field is cheap and gives
exact coverage for antialiasing.
"""

import struct
import sys
import zlib

SIZE = 1024

GREEN = (0x2F, 0x6F, 0x4F)
DARK = (0x24, 0x51, 0x3A)
WHITE = (0xFF, 0xFF, 0xFF)
RING = (0xE8, 0xE4, 0xD8)
MUTED = (0xB4, 0xC6, 0xBA)


def rounded_rect_distance(px, py, x0, y0, x1, y1, radius):
    """Signed distance from a point to a rounded rectangle; negative is inside."""
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    hx, hy = (x1 - x0) / 2 - radius, (y1 - y0) / 2 - radius
    dx = abs(px - cx) - hx
    dy = abs(py - cy) - hy
    ox, oy = max(dx, 0.0), max(dy, 0.0)
    return (ox * ox + oy * oy) ** 0.5 + min(max(dx, dy), 0.0) - radius


def coverage(distance):
    """Convert a distance to 0..1 pixel coverage across a one-pixel edge."""
    return min(max(0.5 - distance, 0.0), 1.0)


def over(base, colour, alpha):
    if alpha <= 0:
        return base
    if alpha >= 1:
        return colour
    return tuple(int(round(c * alpha + b * (1 - alpha))) for c, b in zip(colour, base))


def write_png(path, rows):
    raw = b"".join(b"\x00" + bytes(v for px in row for v in px) for row in rows)

    def chunk(kind, payload):
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 2, 0, 0, 0)  # 8-bit RGB
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.write(chunk(b"IHDR", header))
        fh.write(chunk(b"IDAT", zlib.compress(raw, 9)))
        fh.write(chunk(b"IEND", b""))


def build():
    # Two hanger rings, then the page, then the header band over its top edge,
    # then the day grid with one day blocked out.
    rings = [(336, 176, 388, 300), (636, 176, 688, 300)]
    page = (200, 250, 824, 852)
    header = (200, 250, 824, 424)

    dots = []
    for col in range(3):
        for row in range(2):
            x = 268 + col * 168
            y = 500 + row * 152
            dots.append((x, y, x + 96, y + 96, row == 0 and col == 1))

    rows = []
    for py in range(SIZE):
        line = []
        for px in range(SIZE):
            colour = over(GREEN, GREEN, 1.0)
            colour = over((0, 0, 0), GREEN,
                          coverage(rounded_rect_distance(px, py, 8, 8, SIZE - 8, SIZE - 8, 228)))

            for x0, y0, x1, y1 in rings:
                colour = over(colour, RING,
                              coverage(rounded_rect_distance(px, py, x0, y0, x1, y1, 26)))

            colour = over(colour, WHITE,
                          coverage(rounded_rect_distance(px, py, *page, 68)))

            # The band is drawn as a rounded rect plus a square-off strip so its
            # bottom corners stay flush with the page beneath it.
            band = min(rounded_rect_distance(px, py, *header, 68),
                       rounded_rect_distance(px, py, 200, 360, 824, 424, 0))
            colour = over(colour, DARK, coverage(band))

            for x0, y0, x1, y1, blocked in dots:
                shade = GREEN if blocked else MUTED
                colour = over(colour, shade,
                              coverage(rounded_rect_distance(px, py, x0, y0, x1, y1, 22)))

            line.append(colour)
        rows.append(line)
    return rows


if __name__ == "__main__":
    write_png(sys.argv[1] if len(sys.argv) > 1 else "icon.png", build())
