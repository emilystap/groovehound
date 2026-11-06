from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from groovehound import listens
from groovehound.brainz.http import ServiceError, ServicePause
from groovehound.db import OWNER_ID, LibraryRelease, Listen, Store
from groovehound.web import create_app

from .conftest import FakeOpener, settings_for
from .test_brainz import lb_client, params
from .test_follows import store_at
from .test_library import FOUND_AT, R_CD, R_NO_GROUP, RG_BRAT, release
from .test_search import ARTIST

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
USER = "Rob"
OTHER = "0a1b2c3d-0000-4000-8000-0000000000aa"
RG_CRASH = "0a1b2c3d-0000-4000-8000-0000000000b2"
RG_SUCKER = "0a1b2c3d-0000-4000-8000-0000000000b3"
RG_OTHER = "0a1b2c3d-0000-4000-8000-0000000000b4"
R_CRASH = "0a1b2c3d-0000-4000-8000-0000000000c4"
R_SUCKER = "0a1b2c3d-0000-4000-8000-0000000000c5"
R_OTHER = "0a1b2c3d-0000-4000-8000-0000000000c6"


def entry(mbid: str | None, listens: int, name: str = "") -> dict:
    return {
        "artist_mbids": [ARTIST],
        "artist_name": "Charli xcx",
        "listen_count": listens,
        "release_group_mbid": mbid,
        "release_group_name": name,
    }


def stats(*entries: dict, total: int | None = None, offset: int = 0) -> dict:
    return {
        "payload": {
            "release_groups": list(entries),
            "count": len(entries),
            "offset": offset,
            "total_release_group_count": len(entries) if total is None else total,
            "range": "all_time",
            "last_updated": 1792919208,  # 2026-10-25
            "user_id": USER,
        }
    }


# the shape of a real response (2026-11-02): one entry ListenBrainz couldn't link
STATS = stats(entry(RG_BRAT, 125, "Brat"), entry(None, 17, "Demon Days"), entry(RG_OTHER, 1))


def fetch(store: Store, opener: FakeOpener, pause: ServicePause | None = None, now=NOW, user=USER):
    return listens.fetch(store, lb_client(opener), pause or ServicePause(), OWNER_ID, user, now=now)


def owning(store: Store, *releases: LibraryRelease) -> None:
    store.replace_library(OWNER_ID, releases, FOUND_AT)


def test_one_request_a_day_for_all_time_stats(tmp_path: Path):
    opener = FakeOpener(STATS, STATS)
    with store_at(tmp_path) as store:
        assert fetch(store, opener) == 2  # the unlinked entry has no release group
        request = opener.requests[0]
        assert request.full_url.startswith(
            "https://api.listenbrainz.org/1/stats/user/Rob/release-groups"
        )
        assert params(request) == {"range": ["all_time"], "count": ["1000"]}
        assert store.listened(OWNER_ID, [RG_BRAT, RG_CRASH, RG_OTHER]) == {
            RG_BRAT: 125,
            RG_OTHER: 1,
        }
        assert fetch(store, opener, now=NOW + timedelta(hours=21)) is None  # not due
        assert fetch(store, opener, now=NOW + timedelta(hours=27)) == 2
        summary = listens.stats(store, "rob")  # the cache key ignores case
        assert summary == listens.Stats(2, 1, "2026-10-25T09:06:48+00:00", summary.fetched_at)
    assert len(opener.requests) == 2


def test_a_long_list_is_paged(tmp_path: Path):
    first = stats(entry(RG_BRAT, 9), entry(RG_CRASH, 5), total=3)
    second = stats(entry(RG_OTHER, 1), total=3, offset=2)
    opener = FakeOpener(first, second)
    with store_at(tmp_path) as store:
        assert fetch(store, opener) == 3
        assert params(opener.requests[1])["offset"] == ["2"]
        assert listens.stats(store, USER).release_groups == 3


def test_paging_stops_at_an_empty_page(tmp_path: Path):
    opener = FakeOpener(stats(entry(RG_BRAT, 9), total=5), stats(total=5, offset=1))
    with store_at(tmp_path) as store:
        assert fetch(store, opener) == 1
    assert len(opener.requests) == 2


def test_nothing_asked_without_a_username(tmp_path: Path):
    opener = FakeOpener()
    with store_at(tmp_path) as store:
        assert fetch(store, opener, user=" ") is None
        assert listens.stats(store, "") is None
        assert listens.never_played(store, OWNER_ID, "", [RG_BRAT]) == set()
    assert opener.requests == []


def test_stats_not_calculated_yet_keep_what_is_known(tmp_path: Path):
    opener = FakeOpener(STATS, b"")  # a 204 has no body
    with store_at(tmp_path) as store:
        fetch(store, opener)
        assert fetch(store, opener, now=NOW + timedelta(hours=27)) == 0
        assert listens.stats(store, USER) is None  # no badges on a guess
        assert store.listened(OWNER_ID, [RG_BRAT]) == {RG_BRAT: 125}


def test_an_unknown_user_is_remembered_without_a_pause(tmp_path: Path):
    opener = FakeOpener(STATS, 404)
    pause = ServicePause()
    with store_at(tmp_path) as store:
        fetch(store, opener, pause)
        assert fetch(store, opener, pause, now=NOW + timedelta(hours=27)) == 0
        assert not pause.remaining()
        assert store.listened(OWNER_ID, [RG_BRAT]) == {}
        assert fetch(store, opener, pause, now=NOW + timedelta(hours=28)) is None


def test_an_error_pauses_listenbrainz(tmp_path: Path):
    opener = FakeOpener(500)
    pause = ServicePause()
    with store_at(tmp_path) as store:
        with pytest.raises(ServiceError):
            fetch(store, opener, pause)
        assert pause.remaining()
        with pytest.raises(ServiceError, match="paused"):
            fetch(store, opener, pause)
        assert listens.due(store, USER, now=NOW)
    assert len(opener.requests) == 1


def test_never_played_is_library_minus_played(tmp_path: Path):
    with store_at(tmp_path) as store:
        owning(
            store,
            release(R_CD, RG_BRAT, "Brat"),
            release(R_CRASH, RG_CRASH, "Crash"),
            release(R_SUCKER, RG_SUCKER, "Sucker"),
            release(R_NO_GROUP, None, "Untagged group"),  # can't be matched: never listed
            LibraryRelease(R_OTHER, RG_OTHER, (OTHER,), "Other", "Other One", "Other", 1),
        )
        # no stats yet: nothing marked
        assert listens.never_played(store, OWNER_ID, USER, [RG_BRAT, RG_CRASH]) == set()
        assert listens.unplayed_by_artist(store, OWNER_ID, USER) == []

        fetch(store, FakeOpener(STATS))
        assert listens.never_played(store, OWNER_ID, USER, [RG_BRAT, RG_CRASH]) == {RG_CRASH}
        [artist] = listens.unplayed_by_artist(store, OWNER_ID, USER)  # Other One played theirs
        assert (artist.name, artist.owned, artist.never_played) == (
            "Charli xcx",
            3,
            ["Crash", "Sucker"],
        )
        assert artist.mbid == ""  # no album artist id in these tags: no link


def test_the_library_page_and_badges(tmp_path: Path):
    settings = settings_for(tmp_path, listenbrainz_user=USER)
    with Store(settings.database_path) as store:
        owning(
            store,
            LibraryRelease(R_CD, RG_BRAT, (ARTIST,), "Brat", "Charli xcx", "Brat", 2),
            LibraryRelease(R_CRASH, RG_CRASH, (ARTIST,), "Crash", "Charli xcx", "Crash", 2),
        )
        fetch(store, FakeOpener(STATS))
    client = TestClient(create_app(settings))
    assert "Never played · 1" in client.get("/library").text
    page = client.get("/library/albums?filter=never-played").text
    assert "1 of 2" in page and "By artist (1)" in page
    assert f'href="/artists/{ARTIST}">Charli xcx</a>' in page
    assert "1 of 2 never played" in page and "Crash" in page and "Brat" not in page
    assert " ".join(page.split()).count("(1 album in your stats)") == 1  # the unlinked one

    page = TestClient(create_app(replace(settings, listenbrainz_user=""))).get("/library").text
    assert "Set your ListenBrainz username" in page


def test_the_listen_history_counts_as_played(tmp_path: Path):
    """The stats may link a song to another album (a single); the history keeps the album the
    scrobbler sent, so a listen there makes it played too."""
    with store_at(tmp_path) as store:
        owning(store, release(R_CD, RG_BRAT, "Brat"), release(R_CRASH, RG_CRASH, "Crash"))
        store.save_listens(OWNER_ID, [Listen(1792000000, "msid", release_group_mbid=RG_CRASH)])
        assert listens.never_played(store, OWNER_ID, USER, [RG_CRASH]) == set()  # no stats yet
        fetch(store, FakeOpener(STATS))
        assert listens.never_played(store, OWNER_ID, USER, [RG_BRAT, RG_CRASH]) == set()
        assert listens.played(store, OWNER_ID, [RG_BRAT, RG_CRASH, RG_SUCKER]) == {
            RG_BRAT,
            RG_CRASH,
        }
        assert listens.unplayed_by_artist(store, OWNER_ID, USER) == []
