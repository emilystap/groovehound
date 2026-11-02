"""Draw the app icons (a record on the accent colour) into web/static/, stdlib only.

    uv run python scripts/make_icons.py

The PNGs are committed; rerun this only to change them. `icon-*.png` are for the browser tab and
launchers that show the icon as it is (a rounded square); `icon-maskable-*.png` fill the whole
square and keep the record inside the central circle that Android's masks never crop (radius
40% of the size), so a round or squircle mask shows it whole.
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "src" / "groovehound" / "web" / "static"

ACCENT = (0x3B, 0x5B, 0xDB)  # --accent in style.css
VINYL = (0x1C, 0x20, 0x26)  # --text
GROOVE = (0x3A, 0x41, 0x4C)
LABEL = (0xFF, 0xC0, 0x78)  # --warning (dark theme)


def _coverage(distance: float) -> float:
    """How much of a pixel a shape covers, from the signed distance (pixels, negative inside) of
    the pixel's centre to the shape's edge: a one-pixel ramp, which antialiases the edge."""
    return min(1.0, max(0.0, 0.5 - distance))


def _rounded_square(x: float, y: float, size: int, radius: float) -> float:
    """Signed distance from (x, y) to a size-by-size square with rounded corners."""
    half = size / 2
    qx, qy = abs(x - half) - (half - radius), abs(y - half) - (half - radius)
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    return outside + min(max(qx, qy), 0.0) - radius


def draw(size: int, maskable: bool) -> bytes:
    """RGBA rows of the icon at `size` pixels."""
    record = size * (0.30 if maskable else 0.36)
    # (outer radius, colour), painted from the outside in
    rings = [
        (record, VINYL),
        (record * 0.86, GROOVE),
        (record * 0.83, VINYL),
        (record * 0.68, GROOVE),
        (record * 0.65, VINYL),
        (record * 0.36, LABEL),
        (record * 0.06, ACCENT),  # the spindle hole shows the background
    ]
    centre = size / 2
    rows = bytearray()
    for py in range(size):
        rows.append(0)  # PNG filter type: none
        for px in range(size):
            x, y = px + 0.5, py + 0.5
            alpha = 1.0 if maskable else _coverage(_rounded_square(x, y, size, size * 0.22))
            colour = ACCENT
            d = math.hypot(x - centre, y - centre)
            for radius, ring in rings:
                a = _coverage(d - radius)
                if a == 0.0:
                    break
                colour = tuple(c + (r - c) * a for c, r in zip(colour, ring, strict=True))
            rows.extend(round(c) for c in colour)
            rows.append(round(alpha * 255))
    return bytes(rows)


def png(size: int, rows: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows, 9))
        + chunk(b"IEND", b"")
    )


def main() -> None:
    for name, size, maskable in [
        ("icon-32.png", 32, False),
        ("icon-192.png", 192, False),
        ("icon-512.png", 512, False),
        ("icon-maskable-192.png", 192, True),
        ("icon-maskable-512.png", 512, True),
    ]:
        (STATIC / name).write_bytes(png(size, draw(size, maskable)))
        print(name)


if __name__ == "__main__":
    main()
