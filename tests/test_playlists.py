from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from groovehound import playlists
from groovehound.brainz.http import ServiceError, ServicePause
from groovehound.db import OWNER_ID, Follow, Store
from groovehound.web import create_app

from .conftest import FakeOpener, settings_for
from .test_brainz import lb_client, params
from .test_follows import store_at

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
USER = "Rob"
EXPLORATION = "55f2a01f-3974-46ca-8461-a8a84886eba4"
OLD_EXPLORATION = "55f2a01f-3974-46ca-8461-a8a84886eb00"
DAILY = "ba776a2e-9009-48a0-8898-e87f1777a067"
EXPIRED = "ba776a2e-9009-48a0-8898-e87f1777a000"
HOPKINS = "0b0c25f4-f31c-46a5-a4fb-ccbf53d663bd"
ROYKSOPP = "1c70a3fc-fa3c-4be1-8b55-c3192db8a884"
GOLDFRAPP = "5e00c39c-9c65-420f-aa26-d68f414a74c6"
REC_1 = "4b8ac19a-3333-4261-9f81-fe5efd5383a0"
REC_2 = "4b8ac19a-3333-4261-9f81-fe5efd5383a1"
REC_3 = "4b8ac19a-3333-4261-9f81-fe5efd5383a2"
REC_4 = "4b8ac19a-3333-4261-9f81-fe5efd5383a3"
RG_SINGULARITY = "0a663aab-76e3-4bf8-8b73-39f5c076bc01"
RG_NEBULOUS = "0a663aab-76e3-4bf8-8b73-39f5c076bc02"


def listed(mbid: str, title: str, source: str, date: str, expires_at: str | None) -> dict:
    """One playlist in the created-for list, as ListenBrainz gives it (checked 2026-11-02)."""
    meta: dict = {"algorithm_metadata": {"source_patch": source}}
    if expires_at:
        meta["expires_at"] = expires_at
    return {
        "playlist": {
            "creator": "listenbrainz",
            "date": date,
            "extension": {
                "https://musicbrainz.org/doc/jspf#playlist": {
                    "additional_metadata": meta,
                    "created_for": "rob",
                    "public": True,
                }
            },
            "identifier": f"https://listenbrainz.org/playlist/{mbid}",
            "title": title,
            "track": [],
        }
    }


LISTING = {
    "count": 25,
    "offset": 0,
    "playlist_count": 46,
    "playlists": [
        # the zone left off, as ListenBrainz sometimes does
        listed(DAILY, "Daily Jams for rob, 2026-11-02 Mon", "daily-jams",
               "2026-11-01T23:04:15+00:00", "2026-11-15T23:04:10.330872"),
        listed(EXPIRED, "Daily Jams for rob, 2026-10-01 Wed", "daily-jams",
               "2026-09-30T23:04:15+00:00", "2026-10-14T23:04:10+00:00"),
        listed(OLD_EXPLORATION, "Weekly Exploration for rob, week of 2026-10-19 Mon",
               "weekly-exploration", "2026-10-18T22:13:00+00:00", "2026-11-08T22:12:57+00:00"),
        listed(EXPLORATION, "Weekly Exploration for rob, week of 2026-10-26 Mon",
               "weekly-exploration", "2026-10-25T22:13:00+00:00", "2026-11-15T22:12:57+00:00"),
        listed("ba776a2e-9009-48a0-8898-e87f17772025", "Top Discoveries of 2025 for rob",
               "top-discoveries-of-2025", "2026-01-01T00:00:00+00:00", None),  # yearly: skipped
    ],
}  # fmt: skip


def track(recording: str, title: str, album: str, *artists: tuple[str, str]) -> dict:
    """One track of a playlist (JSPF), as ListenBrainz gives it: the album only by name."""
    return {
        "album": album,
        "creator": " & ".join(name for _, name in artists),
        "extension": {
            "https://musicbrainz.org/doc/jspf#track": {
                "additional_metadata": {
                    "artists": [
                        {"artist_credit_name": n, "artist_mbid": m, "join_phrase": ""}
                        for m, n in artists
                    ],
                    "caa_release_mbid": "2763165d-02a2-414f-a26c-0362cc537baf",
                },
                "artist_identifiers": [f"https://musicbrainz.org/artist/{m}" for m, _ in artists],
            }
        },
        "identifier": [f"https://musicbrainz.org/recording/{recording}"],
        "title": title,
    }


PLAYLIST = {
    "playlist": {
        "title": "Weekly Exploration for rob, week of 2026-10-26 Mon",
        "track": [
            track(REC_1, "Emerald Rush", "Singularity", (HOPKINS, "Jon Hopkins")),
            track(REC_2, "We Enter", "Nebulous Nights", (ROYKSOPP, "Röyksopp"),
                  (GOLDFRAPP, "Alison Goldfrapp")),
            track(REC_3, "Luminous Beings", "Singularity", (HOPKINS, "Jon Hopkins")),
            track(REC_4, "Unmatched", "Who Knows", (HOPKINS, "Jon Hopkins")),
        ],
    }
}  # fmt: skip


def release(group: str, name: str, credit: str) -> dict:
    return {
        "release": {
            "album_artist_name": credit,
            "mbid": f"{group[:-4]}ffff",
            "name": name,
            "release_group_mbid": group,
            "year": 2026,
        }
    }


METADATA = {
    REC_1: release(RG_SINGULARITY, "Singularity", "Jon Hopkins"),
    REC_2: release(RG_NEBULOUS, "Nebulous Nights", "Röyksopp"),
    REC_3: release(RG_SINGULARITY, "Singularity", "Jon Hopkins"),
    # REC_4: not in the metadata
}
OTHER_PLAYLIST = {"playlist": {"title": "Daily Jams", "track": []}}


def fetch(store: Store, opener: FakeOpener, pause: ServicePause | None = None, now=NOW, user=USER):
    return playlists.fetch(store, lb_client(opener), pause or ServicePause(), user, now=now)


def urls(opener: FakeOpener) -> list[str]:
    return [r.full_url.split("?")[0] for r in opener.requests]


def test_the_list_daily_and_each_playlist_once(tmp_path: Path):
    opener = FakeOpener(LISTING, PLAYLIST, METADATA, OTHER_PLAYLIST, LISTING)
    with store_at(tmp_path) as store:
        assert fetch(store, opener) == 4
        root = "https://api.listenbrainz.org/1"
        assert urls(opener) == [
            f"{root}/user/Rob/playlists/createdfor",
            f"{root}/playlist/{EXPLORATION}",  # the newest exploration, not the older one
            f"{root}/metadata/recording/",
            f"{root}/playlist/{DAILY}",  # no tracks: no metadata request
        ]
        assert params(opener.requests[2]) == {
            "recording_mbids": [",".join([REC_1, REC_2, REC_3, REC_4])],
            "inc": ["release"],
        }
        assert fetch(store, opener, now=NOW + timedelta(hours=21)) is None  # nothing due
        assert fetch(store, opener, now=NOW + timedelta(hours=27)) == 1  # just the list
    assert len(opener.requests) == 5


def test_shown_newest_of_each_recurring_kind_exploration_first(tmp_path: Path):
    with store_at(tmp_path) as store:
        fetch(store, FakeOpener(LISTING, PLAYLIST, METADATA, OTHER_PLAYLIST))
        assert [p.mbid for p in playlists.shown(store, USER, now=NOW)] == [EXPLORATION, DAILY]
        later = NOW + timedelta(days=13, hours=12)  # 2026-11-15 24:00: both expired
        assert playlists.shown(store, USER, now=later) == []


def test_albums_in_playlist_order_with_their_songs(tmp_path: Path):
    with store_at(tmp_path) as store:
        fetch(store, FakeOpener(LISTING, PLAYLIST, METADATA, OTHER_PLAYLIST))
        exploration, daily = playlists.playlists(store, OWNER_ID, "rob", now=NOW)
        assert exploration.title == "Weekly Exploration for rob, week of 2026-10-26 Mon"
        assert exploration.url == f"https://listenbrainz.org/playlist/{EXPLORATION}/"
        singularity, nebulous = exploration.releases
        assert singularity.mbid == RG_SINGULARITY
        assert singularity.songs == ("Emerald Rush", "Luminous Beings")
        assert nebulous.artist_credit == "Röyksopp"
        assert nebulous.artist_mbids == (ROYKSOPP, GOLDFRAPP)
        assert nebulous.artist_names == ("Röyksopp", "Alison Goldfrapp")
        assert exploration.hidden == 1  # REC_4: no release group known
        assert daily.releases == ()

        store.dismiss(OWNER_ID, "release-group", RG_SINGULARITY)  # shared with the home page
        exploration, _ = playlists.playlists(store, OWNER_ID, USER, now=NOW)
        assert [h.mbid for h in exploration.releases] == [RG_NEBULOUS]
        assert exploration.hidden == 3
        assert playlists.listed(store, USER, RG_SINGULARITY)  # still on it: its cover is fine
        assert not playlists.listed(store, USER, "0a663aab-76e3-4bf8-8b73-39f5c076bcff")


def test_a_failed_playlist_is_fetched_on_the_next_wake_up(tmp_path: Path):
    opener = FakeOpener(LISTING, 500, PLAYLIST, METADATA, OTHER_PLAYLIST)
    pause = ServicePause()
    with store_at(tmp_path) as store:
        with pytest.raises(ServiceError):
            fetch(store, opener, pause)
        assert pause.remaining()
        with pytest.raises(ServiceError, match="paused"):
            fetch(store, opener, pause)  # paused: nothing asked
        assert fetch(store, opener, now=NOW + timedelta(hours=2)) == 3  # list not due
    assert len(opener.requests) == 5


def test_expired_playlists_are_dropped_with_the_next_list(tmp_path: Path):
    with store_at(tmp_path) as store:
        fetch(store, FakeOpener(LISTING, PLAYLIST, METADATA, OTHER_PLAYLIST))
        later = NOW + timedelta(days=14)
        assert fetch(store, FakeOpener({"playlists": []}), now=later) == 1
        assert store.cached("listenbrainz", playlists.PLAYLIST_KIND, EXPLORATION) is None
        assert store.cached("listenbrainz", playlists.METADATA_KIND, EXPLORATION) is None


def test_nothing_asked_without_a_username(tmp_path: Path):
    opener = FakeOpener()
    with store_at(tmp_path) as store:
        assert fetch(store, opener, user=" ") is None
        assert playlists.playlists(store, OWNER_ID, "") == []
    assert opener.requests == []


def test_an_unknown_user_is_remembered_without_a_pause(tmp_path: Path):
    opener = FakeOpener(404)
    pause = ServicePause()
    with store_at(tmp_path) as store:
        assert fetch(store, opener, pause) == 1
        assert not pause.remaining()
        assert fetch(store, opener, pause, now=NOW + timedelta(hours=1)) is None
        assert playlists.playlists(store, OWNER_ID, USER) == []
    assert len(opener.requests) == 1


def test_the_discover_page(tmp_path: Path):
    settings = settings_for(tmp_path, listenbrainz_user=USER)
    lasting = {  # the page shows what hasn't expired by the real clock
        "playlists": [
            listed(EXPLORATION, "Weekly Exploration for rob, week of 2026-10-26 Mon",
                   "weekly-exploration", "2026-10-25T22:13:00+00:00", "2099-01-01T00:00:00+00:00")
        ]
    }  # fmt: skip
    with Store(settings.database_path) as store:
        fetch(store, FakeOpener(lasting, PLAYLIST, METADATA))
        store.follow(OWNER_ID, Follow(HOPKINS, "Jon Hopkins"))
        store.save_cover(RG_NEBULOUS, b"jpeg", "image/jpeg", "2099-01-01T00:00:00+00:00")
    client = TestClient(create_app(settings))
    page = client.get("/discover").text
    assert "Weekly Exploration for rob, week of 2026-10-26 Mon" in page
    assert "Singularity" in page and "“Emerald Rush”, “Luminous Beings”" in page
    assert f'action="/artists/{GOLDFRAPP}/follow"' in page
    assert f'action="/artists/{HOPKINS}/unfollow"' in page  # followed already
    assert f'name="release_group" value="{RG_NEBULOUS}"' in page  # Add to wishlist
    assert "1 more track dismissed or not matched" in page
    assert client.get(f"/cover/{RG_NEBULOUS}").status_code == 200  # on a playlist

    page = TestClient(create_app(settings_for(tmp_path))).get("/discover").text
    assert "Set your ListenBrainz username" in page
