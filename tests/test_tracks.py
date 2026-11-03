from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from groovehound import releases, tracks
from groovehound.brainz.http import ServiceError, ServicePause
from groovehound.db import OWNER_ID, LibraryFile, LibraryRelease, Store

from .conftest import FakeOpener
from .test_brainz import mb_client

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
LATER = "2027-01-01T00:00:00+00:00"
RG = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
RG2 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
R = [f"{n:08x}-1111-1111-1111-{'1' * 12}" for n in range(6)]
REC = [f"{n:08x}-3333-3333-3333-{'3' * 12}" for n in range(4)]


def edition(mbid: str, tracks: int | None, **extra) -> dict:
    media = [] if tracks is None else [{"format": "Digital Media", "track-count": tracks}]
    return {"id": mbid, "status": "Official", "media": media, "label-info": [], **extra}


BROWSE = {
    "releases": [
        edition(R[0], 14, date="2021-05-01", title="Deluxe"),
        edition(R[1], 12, date="2020-03-01"),
        edition(R[2], 12, date="2019-01-01", status="Promotion"),  # earliest, but not official
        edition(R[3], 12, date="2020-06-01"),
        edition(R[4], None),  # no media (from before 1g)
    ]
}

RELEASE = {
    "id": R[1],
    "title": "Album",
    "artist-credit": [{"name": "Artist", "joinphrase": ""}],
    "media": [
        {
            "position": 1,
            "format": "CD",
            "title": "",
            "tracks": [
                {
                    "number": "1",
                    "title": "Song",
                    "length": 200000,
                    "recording": {"id": REC[0], "title": "Song"},
                    "artist-credit": [
                        {"name": "Artist", "joinphrase": " feat. "},
                        {"name": "Guest", "joinphrase": ""},
                    ],
                },
                {"number": "2", "position": 2, "recording": {"id": REC[1], "title": "Other"}},
            ],
        },
        {
            "position": 2,
            "format": "CD",
            "title": "Bonus",
            "tracks": [{"number": "1", "title": "Extra", "recording": {"id": REC[2]}}],
        },
    ],
}


def own(store: Store, release: str, rg: str, files: int, *recordings: str) -> None:
    store.save_library_files(
        OWNER_ID,
        [
            LibraryFile(f"{release}/{n}.flac", 1, 1, release, rg, recording_mbid=rec)
            for n, rec in enumerate(recordings)
        ],
    )
    old = [r for r in store.library(OWNER_ID) if r.release_mbid != release]
    store.replace_library(
        OWNER_ID, [*old, LibraryRelease(release, rg, (), "Album", "Artist", release, files)], "x"
    )


def test_default_edition_is_the_earliest_official_with_the_commonest_track_count():
    assert tracks.default_edition(BROWSE) == R[1]
    assert tracks.default_edition({"releases": [edition(R[2], 9, status="Bootleg")]}) == R[2]
    assert tracks.default_edition({"releases": [edition(R[4], None)]}) is None
    assert tracks.default_edition({}) is None
    # a tie: the count of the earliest edition wins
    tied = {"releases": [edition(R[0], 14, date="2021"), edition(R[1], 12, date="2022")]}
    assert tracks.default_edition(tied) == R[0]


def test_partial_albums_from_the_cached_browse_only(tmp_path: Path):
    with Store(tmp_path / "db.sqlite") as store:
        own(store, R[1], RG, 8)
        assert tracks.partial_albums(store, OWNER_ID, [RG]) == {}  # no browse: never fetched
        store.save_cached("musicbrainz", releases.CACHE_KIND, RG, BROWSE, LATER)
        assert tracks.partial_albums(store, OWNER_ID, [RG, RG2]) == {
            RG: tracks.Partial(R[1], 8, 12)
        }
        own(store, R[0], RG, 14)  # the deluxe edition, complete: not partial any more
        assert tracks.partial_albums(store, OWNER_ID, [RG]) == {}


def test_partial_albums_skip_editions_the_browse_lacks(tmp_path: Path):
    with Store(tmp_path / "db.sqlite") as store:
        store.save_cached("musicbrainz", releases.CACHE_KIND, RG, BROWSE, LATER)
        own(store, R[4], RG, 3)  # no media in the browse
        own(store, R[5], RG, 3)  # not in the browse at all
        assert tracks.partial_albums(store, OWNER_ID, [RG]) == {}


def test_pick_edition(tmp_path: Path):
    with Store(tmp_path / "db.sqlite") as store:
        assert tracks.pick_edition(store, OWNER_ID, RG) is None
        assert tracks.pick_edition(store, OWNER_ID, RG, browse=BROWSE) == tracks.Pick(
            R[1], "default"
        )
        assert tracks.pick_edition(store, OWNER_ID, RG, R[3], BROWSE) == tracks.Pick(
            R[3], "wishlist"
        )
        own(store, R[0], RG, 14)
        assert tracks.pick_edition(store, OWNER_ID, RG, R[3], BROWSE) == tracks.Pick(
            R[0], "library"
        )


def test_track_list_marks_songs_owned_on_any_release(tmp_path: Path):
    with Store(tmp_path / "db.sqlite") as store:
        own(store, R[5], RG2, 1, REC[1])  # the second song, on a compilation
        listed = tracks.track_list(store, OWNER_ID, RELEASE)
        assert (listed.title, listed.artist, listed.total, listed.owned) == (
            "Album",
            "Artist",
            3,
            1,
        )
        first, second = listed.media
        assert (second.position, second.format, second.title) == (2, "CD", "Bonus")
        song, other = first.tracks
        assert song == tracks.Track("1", "Song", "Artist feat. Guest", 200000, REC[0], False)
        assert (other.title, other.length_ms, other.owned) == ("Other", None, True)
        assert tracks.track_list(store, OWNER_ID, {}) is None


def test_fetch_asks_once_then_from_the_cache_until_due(tmp_path: Path):
    opener = FakeOpener(RELEASE, RELEASE)
    client = mb_client(opener)
    with Store(tmp_path / "db.sqlite") as store:
        assert tracks.fetch(store, client, ServicePause(), R[1], now=NOW) == RELEASE
        assert tracks.fetch(store, client, ServicePause(), R[1], now=NOW) == RELEASE
        assert len(opener.requests) == 1
        url = opener.requests[0].full_url
        assert f"/release/{R[1]}?" in url and "inc=recordings+artist-credits" in url
        tracks.fetch(store, client, ServicePause(), R[1], now=NOW + timedelta(days=61))
        assert len(opener.requests) == 2


def test_fetch_remembers_a_missing_release_and_pauses_on_errors(tmp_path: Path):
    with Store(tmp_path / "db.sqlite") as store:
        opener = FakeOpener(404)
        assert tracks.fetch(store, mb_client(opener), ServicePause(), R[1], now=NOW) == {}
        assert tracks.fetch(store, mb_client(opener), ServicePause(), R[1], now=NOW) == {}
        assert len(opener.requests) == 1

        pause = ServicePause()
        failing = mb_client(FakeOpener(400))
        with pytest.raises(ServiceError):
            tracks.fetch(store, failing, pause, R[2], now=NOW)
        assert pause.remaining()
        with pytest.raises(ServiceError, match="paused"):
            tracks.fetch(store, failing, pause, R[2], now=NOW)
