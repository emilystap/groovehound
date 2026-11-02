from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from groovehound import follows, reconcile
from groovehound.brainz import musicbrainz
from groovehound.brainz.http import ServicePause
from groovehound.db import OWNER_ID, Store
from groovehound.web import create_app

from .conftest import FakeOpener, settings_for
from .test_brainz import mb_client, params
from .test_follow_types import SINGLE, SINGLES
from .test_follows import ARTIST, BROWSE, NOW, group

OTHER = "0a1b2c3d-0000-4000-8000-0000000000aa"
RG_NEW = "0a1b2c3d-0000-4000-8000-0000000000bb"
OTHER_BROWSE = {
    "release-group-count": 1,
    "release-groups": [
        group(
            "0a1b2c3d-0000-4000-8000-0000000000cc",
            "Other",
            "2020",
            credit=[{"name": "Other", "artist": {"id": OTHER, "name": "Other"}}],
        )
    ],
}
LATER = NOW + timedelta(days=15)  # past every browse's 7-14 days
NEWER = {
    "release-group-count": 5,
    "release-groups": [*BROWSE["release-groups"], group(RG_NEW, "Brat 2", "2026-11-20")],
}


def store_at(tmp_path: Path) -> Store:
    return Store(tmp_path / "groovehound.db")


def follow(store: Store, opener: FakeOpener, mbid: str = ARTIST) -> None:
    follows.follow(store, mb_client(opener), ServicePause(), OWNER_ID, mbid, now=NOW)


def run(store: Store, opener: FakeOpener, pause: ServicePause | None = None, **kwargs):
    return reconcile.reconcile_once(
        store, mb_client(opener), pause or ServicePause(), now=LATER, **kwargs
    )


def test_a_due_browse_finds_new_release_groups(tmp_path: Path):
    opener = FakeOpener(BROWSE, NEWER)
    with store_at(tmp_path) as store:
        follow(store, opener)
        assert run(store, opener) == reconcile.Result(browsed=1, new=1)
        seen = store.release_groups_seen(ARTIST)
        followed = store.follow_of(OWNER_ID, ARTIST)
        refreshed = store.cached("musicbrainz", follows.CACHE_KIND, ARTIST)
    assert seen[RG_NEW] == LATER.isoformat()  # new, as of this browse
    assert seen[RG_NEW] > followed.followed_at
    assert refreshed.fetched_at == LATER.isoformat() and refreshed.refresh_after > LATER.isoformat()
    assert params(opener.requests[1])["type"] == ["album|ep"]


def test_nothing_due_asks_nothing(tmp_path: Path):
    opener = FakeOpener(BROWSE)
    with store_at(tmp_path) as store:
        follow(store, opener)
        result = reconcile.reconcile_once(
            store, mb_client(opener), ServicePause(), now=NOW + timedelta(days=6)
        )
    assert result == reconcile.Result() and len(opener.requests) == 1


def test_a_batch_is_bounded_and_takes_the_longest_due_first(tmp_path: Path):
    opener = FakeOpener(BROWSE, OTHER_BROWSE, OTHER_BROWSE)
    with store_at(tmp_path) as store:
        follow(store, opener)
        follow(store, opener, OTHER)
        store.save_cached(
            "musicbrainz",
            follows.CACHE_KIND,
            OTHER,
            OTHER_BROWSE,
            (NOW + timedelta(days=1)).isoformat(),
        )
        assert run(store, opener, batch=1).browsed == 1
        assert params(opener.requests[2])["artist"] == [OTHER]


def test_singles_are_rebrowsed_only_for_artists_followed_for_them(tmp_path: Path):
    newer_singles = {
        "release-group-count": 2,
        "release-groups": [
            *SINGLES["release-groups"],
            {**group(RG_NEW, "360", "2026-11-20"), "primary-type": "Single"},
        ],
    }
    opener = FakeOpener(BROWSE, SINGLES, BROWSE, newer_singles)
    with store_at(tmp_path) as store:
        follow(store, opener)
        follows.set_types(
            store, mb_client(opener), ServicePause(), OWNER_ID, ARTIST, ("album", "single"),
            now=NOW,
        )  # fmt: skip
        assert run(store, opener) == reconcile.Result(browsed=2, new=1)
        assert store.release_groups_seen(ARTIST)[SINGLE] == NOW.isoformat()
    assert [params(r)["type"] for r in opener.requests[2:]] == [["album|ep"], ["single"]]


def test_unfollowed_artists_are_left_alone(tmp_path: Path):
    opener = FakeOpener(BROWSE)
    with store_at(tmp_path) as store:
        follow(store, opener)
        store.unfollow(OWNER_ID, ARTIST)
        assert run(store, opener) == reconcile.Result()


def test_an_error_stops_the_batch_and_pauses_musicbrainz(tmp_path: Path):
    opener = FakeOpener(BROWSE, OTHER_BROWSE, 500, 500, 500)
    pause = ServicePause()
    with store_at(tmp_path) as store:
        follow(store, opener)
        follow(store, opener, OTHER)
        result = run(store, opener, pause)
        assert result.browsed == 0 and result.error
        assert pause.remaining()
        requests = len(opener.requests)
        assert run(store, opener, pause).error  # paused: asks nothing
    assert len(opener.requests) == requests


def test_the_app_runs_the_trickle_only_with_a_contact(tmp_path: Path, monkeypatch):
    started = []
    monkeypatch.setattr(reconcile.Reconciler, "start", lambda self: started.append(self))
    with TestClient(create_app(settings_for(tmp_path))):
        pass
    with TestClient(create_app(replace(settings_for(tmp_path), contact=""))):
        pass
    assert len(started) == 1


def test_the_worker_uses_the_shared_client_and_pause(tmp_path: Path, monkeypatch):
    opener = FakeOpener(BROWSE, NEWER)
    with store_at(tmp_path) as store:
        follow(store, opener)
        store.save_cached("musicbrainz", follows.CACHE_KIND, ARTIST, BROWSE, NOW.isoformat())
    monkeypatch.setattr(musicbrainz, "client_for", lambda settings: mb_client(opener))
    result = reconcile.Reconciler(settings_for(tmp_path)).run_once()
    assert result == reconcile.Result(browsed=1, new=1)
