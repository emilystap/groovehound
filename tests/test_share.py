from pathlib import Path

import pytest

from groovehound.share import parse_share

FIXTURES = Path(__file__).parent / "fixtures"


def now_playing_shares() -> list[str]:
    """The recorded shares, one text per blank-line separated block."""
    text = (FIXTURES / "now_playing_shares.txt").read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if not line.startswith("#")]
    return [block.strip() for block in "\n".join(lines).split("\n\n") if block.strip()]


def test_recorded_now_playing_shares():
    shares = now_playing_shares()
    assert [parse_share(s) for s in shares] == [
        ("What You Waiting For?", "Gwen Stefani"),
        ("DARE", "Gorillaz"),
        ("In This Bih'", "Chris Lorenzo"),
        ("Danger", "Unloved"),
    ]


def test_splits_at_the_last_by():
    assert parse_share("Stand by Me by Ben E. King") == ("Stand by Me", "Ben E. King")


@pytest.mark.parametrize(
    "fields",
    [
        ("", "", "https://www.google.com/search?q=DARE+by+Gorillaz"),  # only the link
        ("Now Playing", "DARE by Gorillaz", ""),  # a title field that isn't the song
        ("", "DARE by Gorillaz\nhttps://www.google.com/search?q=DARE+by+Gorillaz", ""),
    ],
)
def test_fields_in_any_mix(fields):
    assert parse_share(*fields) == ("DARE", "Gorillaz")


@pytest.mark.parametrize(
    "fields",
    [
        ("song with the saxophone at the café",),
        ("", "", "https://example.com/some/page"),
        ("by Gorillaz",),
        ("DARE by ",),
        ("", "   ", ""),
    ],
)
def test_what_does_not_parse(fields):
    assert parse_share(*fields) is None
