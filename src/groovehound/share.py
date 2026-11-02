"""Share to search (roadmap 1f): what Android's share sheet sends, read as a song and artist.

Aimed at Pixel's Now Playing, whose share (from its history) is two lines, as recorded in
`tests/fixtures/now_playing_shares.txt`:

    DARE by Gorillaz
    https://www.google.com/search?q=DARE+by+Gorillaz

Everything here is local: the link is read, never fetched.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from .search import clean

BY = " by "


def parse_share(*fields: str) -> tuple[str, str] | None:
    """(title, artist) from a share's fields (title, text, url, in any mix), or None when they
    don't read as "<title> by <artist>". The first line that isn't a link and has " by " in it
    is split at the last one (a title can contain one: "Stand by Me by Ben E. King"); failing
    that, the same words are taken from a link's `q` parameter. The fields' order and which
    holds what isn't relied on: apps fill them differently."""
    lines = [line.strip() for field in fields for line in field.splitlines() if line.strip()]
    candidates = [line for line in lines if not _is_link(line)]
    candidates += [q for line in lines if (q := _query(line))]
    for line in candidates:
        if found := _split(line):
            return found
    return None


def _split(line: str) -> tuple[str, str] | None:
    title, by, artist = clean(line).rpartition(BY)
    if not by or not title.strip() or not artist.strip():
        return None
    return clean(title), clean(artist)


def _is_link(line: str) -> bool:
    return line.startswith(("http://", "https://"))


def _query(line: str) -> str:
    """A link's `q` parameter ("" without one)."""
    if not _is_link(line):
        return ""
    return parse_qs(urlsplit(line).query).get("q", [""])[0]
