import io

from PIL import Image

from groovehound import tint


def picture(*colours: str) -> bytes:
    """A JPEG in vertical stripes of these colours, equally wide."""
    image = Image.new("RGB", (60 * len(colours), 60))
    for i, colour in enumerate(colours):
        image.paste(colour, (60 * i, 0, 60 * (i + 1), 60))
    out = io.BytesIO()
    image.save(out, "JPEG")
    return out.getvalue()


def close_to(colour: str, expected: str, within: int = 12) -> bool:
    pairs = zip(tint._rgb(colour), tint._rgb(expected), strict=True)
    return all(abs(a - b) * 255 <= within for a, b in pairs)


def test_the_main_colour_isnt_near_black_or_white():
    assert close_to(tint.main_colour(picture("#3366cc")), "#3366cc")
    # black covers most of it, then white: the blue is its colour
    assert close_to(tint.main_colour(picture("black", "black", "white", "#3366cc")), "#3366cc")
    assert tint.main_colour(picture("black", "white")) == ""
    assert tint.main_colour(b"not an image") == ""


def test_bands_pass_aa_in_both_themes():
    for colour in ("#3366cc", "#ffee00", "#ff0000", "#808080", "#0a1a0a", "#f0f8f0"):
        assert tint.contrast(tint.band(colour, dark=True), tint.DARK_TEXT) >= tint.AA
        assert tint.contrast(tint.band(colour, dark=False), tint.LIGHT_TEXT) >= tint.AA
    assert tint.band("", dark=True) == ""


def test_a_blue_cover_gives_the_mockups_bands():
    assert close_to(tint.band("#2f6f9f", dark=True), "#22313b", within=16)
    assert close_to(tint.band("#2f6f9f", dark=False), "#dfe9f0", within=16)
