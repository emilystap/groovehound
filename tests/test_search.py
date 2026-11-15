from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from groovehound.brainz.http import ServiceError, ServicePause
from groovehound.db import Store
from groovehound.search import (
    SEARCH_TTL,
    Hit,
    Reading,
    artist_hits,
    clean,
    from_saved,
    read,
    recording_hits,
    release_group_hits,
    rerank,
    saved_text,
    search,
)

from .conftest import FakeClock, FakeOpener
from .test_brainz import mb_client, params

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
RG = "f0d6a0f8-0c8f-4bb4-a6a2-0f8b1f6c2b11"
RG_COMP = "1b0e7c1e-2a41-4d4e-9c8a-5d3c0e1b7a22"
ARTIST = "260b6184-8828-48eb-945c-bc4cb6fc34ca"
GUEST = "5b11f4ce-a62d-471e-81fc-a69a8278c7da"
CREDIT = [
    {"name": "Charli xcx", "joinphrase": " feat. ", "artist": {"id": ARTIST}},
    {"name": "Guest", "joinphrase": "", "artist": {"id": GUEST}},
]
SOLO = [{"name": "Charli xcx", "artist": {"id": ARTIST}}]

RELEASE_GROUPS = {
    "count": 1,
    "release-groups": [
        {
            "id": RG,
            "title": "Brat",
            "primary-type": "Album",
            "secondary-types": ["Live"],
            "first-release-date": "2024-06-07",
            "artist-credit": CREDIT,
            "releases": [{"id": "a" * 8 + "-0000-0000-0000-" + "0" * 12, "title": "Brat"}],
            "tags": [{"name": "not shown", "count": 1}],
        },
        {"title": "no id, skipped"},
    ],
}


def release(mbid: str, rg: dict, **extra) -> dict:
    return {"id": mbid, "release-group": rg, **extra}


ALBUM = {"id": RG, "title": "Brat", "primary-type": "Album"}
COMP = {"id": RG_COMP, "title": "Hits 2024", "primary-type": "Album",
        "secondary-types": ["Compilation"]}  # fmt: skip
R1, R2, R3 = (f"{c * 8}-1111-1111-1111-{'1' * 12}" for c in "bcd")
RECORDINGS = {
    "count": 2,
    "recordings": [
        {
            "id": "rec1",
            "title": "360",
            "artist-credit": SOLO,
            "releases": [
                # a compilation ranked first still comes after the original album
                release(
                    R3,
                    COMP,
                    status="Official",
                    date="2024-12-01",
                    **{"artist-credit": [{"name": "Various Artists"}]},
                ),
                release(
                    R1,
                    ALBUM,
                    status="Official",
                    date="2024-06-07",
                    country="GB",
                    title="Brat",
                    media=[{"format": "CD"}, {"format": "CD"}],
                ),
            ],
        },
        {
            "id": "rec2",
            "title": "360 (remix)",
            "artist-credit": SOLO,
            "releases": [
                release(R2, ALBUM, date="2024-06-01", title="Brat and it's completely different"),
                release(R1, ALBUM, date="2024-06-07"),  # seen already
            ],
        },
    ],
}


def test_release_group_hits():
    (hit,) = release_group_hits(RELEASE_GROUPS)
    assert hit.mbid == RG
    assert hit.artist_credit == "Charli xcx feat. Guest"
    assert hit.artist_mbids == (ARTIST, GUEST)
    assert hit.type_label == "Album · Live"
    assert hit.first_release_date == "2024-06-07"
    assert hit.editions == ()  # the response can't tell them apart: chosen on the wishlist


def test_recording_hits_group_by_album_originals_first():
    album, comp = recording_hits(RECORDINGS)
    assert (album.mbid, comp.mbid) == (RG, RG_COMP)
    assert album.songs == ("360", "360 (remix)")
    assert album.first_release_date == "2024-06-01"  # the earliest release seen
    assert album.artist_credit == "Charli xcx"
    assert [e.mbid for e in album.editions] == [R2, R1]  # oldest first
    assert [e.label for e in album.editions] == [
        "Brat and it's completely different · 2024-06-01",
        "2024-06-07 · GB · 2×CD",  # noqa: RUF001
    ]
    assert comp.artist_credit == "Various Artists"  # the release's own credit
    assert comp.type_label == "Album · Compilation"


def test_clean():
    assert clean("  brat \n  deluxe ") == "brat deluxe"
    assert len(clean("x" * 500)) == 200


def store_at(tmp_path: Path) -> Store:
    return Store(tmp_path / "db.sqlite")


def album(title: str, artist: str = "") -> Reading:
    return read("album", title, artist, box=bool(artist))


def test_search_is_cached_for_a_day(tmp_path: Path):
    opener = FakeOpener(RELEASE_GROUPS)
    client, pause = mb_client(opener), ServicePause()
    with store_at(tmp_path) as store:
        first = search(store, client, pause, album("Brat ", "charli  XCX"), now=NOW)
        again = search(store, client, pause, read("album", "brat by Charli xcx"), now=NOW)
        assert first.hits == again.hits  # split at " by ", the same fields
        (request,) = opener.requests
        assert params(request)["query"] == ['releasegroup:"Brat" AND artist:"charli XCX"']

        songs = FakeOpener(RECORDINGS)  # a song search with the same words is another request
        found = search(store, mb_client(songs), pause, read("song", "brat by charli xcx"), now=NOW)
        assert found.hits and len(songs.requests) == 1

        later = FakeOpener(RELEASE_GROUPS)
        search(store, mb_client(later), pause, album("brat", "charli xcx"), now=NOW + SEARCH_TTL)
        assert len(later.requests) == 1


def test_free_text_is_its_own_search(tmp_path: Path):
    opener = FakeOpener(RELEASE_GROUPS, RELEASE_GROUPS)
    client, pause = mb_client(opener), ServicePause()
    with store_at(tmp_path) as store:
        words = read("album", "brat charli: (xcx) AND more")
        assert words.how == "words"
        search(store, client, pause, words, now=NOW)
        search(
            store, client, pause, read("album", "brat charli: (xcx) and more", box=True), now=NOW
        )
        assert [params(r)["query"] for r in opener.requests] == [
            [r"brat charli\: \(xcx\) and more"],  # escaped, "AND" a word
            ['releasegroup:"brat charli: (xcx) and more"'],  # the box open, though empty
        ]
        search(store, client, pause, words, now=NOW)  # cached
        assert len(opener.requests) == 2


def test_artists_are_searched_by_name(tmp_path: Path):
    opener = FakeOpener(ARTISTS)
    with store_at(tmp_path) as store:
        found = search(
            store, mb_client(opener), ServicePause(), read("artist", "Gorillaz by"), now=NOW
        )
    assert params(opener.requests[0])["query"] == ['artist:"Gorillaz by"']  # never split
    assert opener.requests[0].full_url.split("?")[0].endswith("/ws/2/artist")
    assert [a.name for a in found.artists] == ["Gorillaz", "Jazz Gorillaz"]
    assert found.hits == []


def test_old_searches_are_dropped(tmp_path: Path):
    pause = ServicePause()
    with store_at(tmp_path) as store:
        search(store, mb_client(FakeOpener(RELEASE_GROUPS)), pause, album("old", "x"), now=NOW)
        later = NOW + timedelta(days=30)
        search(store, mb_client(FakeOpener(RELEASE_GROUPS)), pause, album("new", "x"), now=later)
        assert store.cached("musicbrainz", "search-release-group", "oldx") is None


def test_failure_pauses_searching_but_not_the_cache(tmp_path: Path):
    clock = FakeClock()
    pause = ServicePause(clock=clock)
    with store_at(tmp_path) as store:
        search(
            store, mb_client(FakeOpener(RELEASE_GROUPS)), pause, read("album", "cached"), now=NOW
        )
        with pytest.raises(ServiceError):
            search(store, mb_client(FakeOpener(500)), pause, read("album", "fails"), now=NOW)
        assert pause.remaining() == 3600
        refused = FakeOpener()
        with pytest.raises(ServiceError, match="paused"):
            search(store, mb_client(refused), pause, read("album", "other"), now=NOW)
        assert refused.requests == []
        assert search(store, mb_client(refused), pause, read("album", "cached"), now=NOW).hits
        clock.now += 3600
        search(store, mb_client(FakeOpener(RELEASE_GROUPS)), pause, read("album", "other"), now=NOW)


@pytest.mark.parametrize(
    ("args", "kw", "expected"),
    [
        (("album", "Demon Days by Gorillaz"), {}, Reading("album", "Demon Days", "Gorillaz", "by")),
        (("album", "Stand by Me by Ben E. King"), {},
         Reading("album", "Stand by Me", "Ben E. King", "by")),
        (("album", "Stand by Me by Ben E. King"), {"words": True},
         Reading("album", "Stand by Me by Ben E. King", how="words")),
        (("album", "demon  days gorillaz"), {},
         Reading("album", "demon days gorillaz", how="words")),
        (("song", "feel good inc"), {}, Reading("song", "feel good inc", how="title")),
        (("song", "Stand by Me", "Ben E. King"), {"box": True},
         Reading("song", "Stand by Me", "Ben E. King", "fields")),
        (("album", "brat", "ignored"), {}, Reading("album", "brat", how="words")),  # box closed
        (("artist", "Simon by Garfunkel", "x"), {"box": True},
         Reading("artist", "Simon by Garfunkel", how="name")),
    ],
)  # fmt: skip
def test_reading_what_was_typed(args, kw, expected):
    assert read(*args, **kw) == expected


def test_search_rejects_bad_input():
    with pytest.raises(ValueError):
        read("label", "x")
    with pytest.raises(ValueError):
        read("album", "   ")


def hit(title: str, score: int, primary: str = "Album", *secondary: str, n: int = 1) -> Hit:
    return Hit(title, title, "", primary_type=primary, secondary_types=secondary, score=score,
               release_count=n)  # fmt: skip


def test_rerank_puts_the_album_first():
    # "demon days gorillaz" as free text, as a mirror answered it on 2026-11-14
    found = rerank([
        hit("remix", 100, "Single", "Remix"),
        hit("single", 57, "Single"),
        hit("album", 57, n=32),
        hit("live", 40, "Album", "Live", n=3),
        hit("someone else", 35, "Single"),
        hit("ep", 52, "EP"),
        hit("other", 55, "Other"),
    ])  # fmt: skip
    assert [h.title for h in found] == [
        "album", "ep", "single", "other", "remix", "live", "someone else"
    ]  # fmt: skip
    assert rerank([]) == []


def test_rerank_breaks_ties_by_editions():
    found = rerank([hit("few", 100, n=1), hit("many", 100, n=39), hit("higher", 100, n=39)])
    assert [h.title for h in found] == ["many", "higher", "few"]


def test_release_group_hits_keep_score_and_editions():
    data = {"release-groups": [{"id": RG, "title": "Brat", "score": "97", "count": 25}]}
    (found,) = release_group_hits(data)
    assert (found.score, found.release_count) == (97, 25)


ARTISTS = {
    "count": 2,
    "artists": [
        {"id": ARTIST, "name": "Gorillaz", "type": "Group", "country": "GB",
         "disambiguation": "virtual band", "score": 100},
        {"id": GUEST, "name": "Jazz Gorillaz", "score": 68},
        {"name": "no id, skipped"},
    ],
}  # fmt: skip


def test_artist_hits():
    first, second = artist_hits(ARTISTS)
    assert first.detail == "Group · GB · virtual band"
    assert second.detail == ""


def test_saved_text_and_back():
    assert saved_text(" sax  song ") == "sax song"
    assert saved_text("360", "Charli xcx", "song") == "360 by Charli xcx (song)"
    assert saved_text("gorillaz", kind="artist") == "gorillaz (artist)"
    assert saved_text("", "") == "" and saved_text("", kind="song") == ""
    assert from_saved("360 by Charli xcx (song)") == ("360 by Charli xcx", "song")
    assert from_saved("Brat (Album)") == ("Brat", "album")  # as saved before the one bar
    assert from_saved("song with a (sax) solo") == ("song with a (sax) solo", "album")
