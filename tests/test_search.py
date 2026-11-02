from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from groovehound.brainz.http import ServiceError, ServicePause
from groovehound.db import Store
from groovehound.search import (
    SEARCH_TTL,
    clean,
    recording_hits,
    release_group_hits,
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
    assert [e.label for e in hit.editions] == ["Brat"]


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


def test_search_is_cached_for_a_day(tmp_path: Path):
    opener = FakeOpener(RELEASE_GROUPS)
    client, pause = mb_client(opener), ServicePause()
    with store_at(tmp_path) as store:
        first = search(store, client, pause, "album", "Brat ", "charli  XCX", now=NOW)
        again = search(store, client, pause, "album", "brat", "Charli xcx", now=NOW)
        assert first == again
        (request,) = opener.requests
        assert params(request)["query"] == ['releasegroup:"Brat" AND artist:"charli XCX"']

        songs = FakeOpener(RECORDINGS)  # a song search with the same words is another request
        assert search(store, mb_client(songs), pause, "song", "brat", "charli xcx", now=NOW)
        assert len(songs.requests) == 1

        later = FakeOpener(RELEASE_GROUPS)
        search(store, mb_client(later), pause, "album", "brat", "charli xcx", now=NOW + SEARCH_TTL)
        assert len(later.requests) == 1


def test_old_searches_are_dropped(tmp_path: Path):
    pause = ServicePause()
    with store_at(tmp_path) as store:
        search(store, mb_client(FakeOpener(RELEASE_GROUPS)), pause, "album", "old", now=NOW)
        later = NOW + timedelta(days=30)
        search(store, mb_client(FakeOpener(RELEASE_GROUPS)), pause, "album", "new", now=later)
        assert store.cached("musicbrainz", "search-release-group", "old\x1f") is None


def test_failure_pauses_searching_but_not_the_cache(tmp_path: Path):
    clock = FakeClock()
    pause = ServicePause(clock=clock)
    with store_at(tmp_path) as store:
        search(store, mb_client(FakeOpener(RELEASE_GROUPS)), pause, "album", "cached", now=NOW)
        with pytest.raises(ServiceError):
            search(store, mb_client(FakeOpener(500)), pause, "album", "fails", now=NOW)
        assert pause.remaining() == 3600
        refused = FakeOpener()
        with pytest.raises(ServiceError, match="paused"):
            search(store, mb_client(refused), pause, "album", "other", now=NOW)
        assert refused.requests == []
        assert search(store, mb_client(refused), pause, "album", "cached", now=NOW)
        clock.now += 3600
        search(store, mb_client(FakeOpener(RELEASE_GROUPS)), pause, "album", "other", now=NOW)


def test_search_rejects_bad_input(tmp_path: Path):
    with store_at(tmp_path) as store:
        with pytest.raises(ValueError):
            search(store, mb_client(FakeOpener()), ServicePause(), "artist", "x")
        with pytest.raises(ValueError):
            search(store, mb_client(FakeOpener()), ServicePause(), "album", "   ")
