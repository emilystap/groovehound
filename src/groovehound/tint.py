"""A cover's tint (docs/ui-roadmap.md, "Direction A, as mocked up"): the album page's header
sits on a band coloured from its cover.

`main_colour` is worked out once, when a cover is cached (`covers.py`), and stored with it: the
cover reduced to a few colours, the most common one that isn't near black or white. `band` turns
it into the header's colour for a theme: a dark tone of it under the dark theme's text, a light
one under the light theme's, each checked to pass WCAG AA (4.5:1) against that text.
"""

from __future__ import annotations

import colorsys
import io

from PIL import Image

COLOURS = 8  # the cover is reduced to this many colours
MIN_SHARE = 0.05  # a colour covering less of the cover than this isn't its main colour
# Lightness (HLS, 0-1) outside these is near black or white, which makes no band
DARKEST, LIGHTEST = 0.1, 0.92

DARK_TEXT, LIGHT_TEXT = "#f4f4f4", "#111111"  # --text in each theme
# The band's lightness and most saturation in each theme (the mockups' #22313b and #dfe9f0)
DARK_BAND = (0.18, 0.4)
LIGHT_BAND = (0.91, 0.5)
AA = 4.5


def main_colour(image: bytes) -> str:
    """The cover's main colour as "#rrggbb", or "" if it has none that isn't near black or
    white (or it can't be read)."""
    try:
        with Image.open(io.BytesIO(image)) as opened:
            opened.draft("RGB", (64, 64))  # JPEGs decode at a fraction of their size
            small = opened.convert("RGB")
    except (OSError, ValueError, Image.DecompressionBombError):
        return ""
    small.thumbnail((64, 64))
    reduced = small.quantize(colors=COLOURS, method=Image.Quantize.MEDIANCUT)
    palette = reduced.getpalette() or []
    pixels = small.width * small.height
    for count, index in sorted(reduced.getcolors() or [], reverse=True):
        if count < pixels * MIN_SHARE:
            break
        red, green, blue = palette[index * 3 : index * 3 + 3]
        _, lightness, _ = colorsys.rgb_to_hls(red / 255, green / 255, blue / 255)
        if DARKEST <= lightness <= LIGHTEST:
            return f"#{red:02x}{green:02x}{blue:02x}"
    return ""


def band(tint: str, *, dark: bool) -> str:
    """The header band's colour for a cover's `tint` in the dark or light theme; "" for no tint
    (the page then uses --surface)."""
    if not tint:
        return ""
    hue, _, saturation = colorsys.rgb_to_hls(*_rgb(tint))
    lightness, most = DARK_BAND if dark else LIGHT_BAND
    text = DARK_TEXT if dark else LIGHT_TEXT
    step = -0.01 if dark else 0.01
    while True:
        colour = _hex(colorsys.hls_to_rgb(hue, lightness, min(saturation, most)))
        if contrast(colour, text) >= AA or not 0 < lightness < 1:
            return colour
        lightness = min(max(lightness + step, 0), 1)


def contrast(first: str, second: str) -> float:
    """The WCAG contrast ratio of two "#rrggbb" colours, 1 to 21."""
    lighter, darker = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _luminance(colour: str) -> float:
    def linear(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    red, green, blue = (linear(c) for c in _rgb(colour))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _rgb(colour: str) -> tuple[float, float, float]:
    return tuple(int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5))


def _hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{round(c * 255):02x}" for c in rgb)
