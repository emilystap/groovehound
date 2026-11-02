from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from groovehound import follows, personal, reconcile
from groovehound.brainz import listenbrainz, musicbrainz
from groovehound.brainz.http import ServiceError, ServicePause
from groovehound.db import OWNER_ID, Follow, Store
from groovehound.web import create_app

from .conftest import FakeOpener, settings_for
from .test_brainz import lb_client, mb_client, params
from .test_follows import store_at
from .test_fresh import OTHER, RG_ELSE, RG_FRESH, RG_SOON, release
from .test_search import ARTIST

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
USER = "Rob"
DUO = "0a1b2c3d-0000-4000-8000-0000000000bb"
VARIOUS = "89ad4ac3-39f7-470e-963a-56509c546377"
RG_DUO = "0a1b2c3d-0000-4000-8000-000000000024"
RG_VA = "0a1b2c3d-0000-4000-8000-000000000025"

FEED = {
    "payload": {
        "releases": [
            release(RG_ELSE, "Someone Else", "2026-10-30", OTHER, confidence=2,
                    artist_credit_name="Other One"),
            release(RG_ELSE, "Someone Else", "2026-10-27", OTHER, confidence=1,
                    artist_credit_name="Other One"),  # an earlier edition
            release(RG_DUO, "Together", "2026-11-12", OTHER, DUO, confidence=5,
                    artist_credit_name="Other One & The Duo"),
            release(RG_FRESH, "Fresh One", "2026-10-30", confidence=9),  # followed: not shown
            release(RG_VA, "Hits", "2026-10-30", VARIOUS, confidence=9,
                    artist_credit_name="Various Artists"),
        ]
    }
}  # fmt: skip


def fetch(store: Store, opener: FakeOpener, pause: ServicePause | None = None, now=NOW, user=USER):
    return personal.fetch(store, lb_client(opener), pause or ServicePause(), user, now=now)


def test_one_request_a_day_for_the_configured_user(tmp_path: Path):
    opener = FakeOpener(FEED, FEED)
    with store_at(tmp_path) as store:
        assert fetch(store, opener) == 5
        assert opener.requests[0].full_url.startswith(
            "https://api.listenbrainz.org/1/user/Rob/fresh_releases"
        )
        assert params(opener.requests[0])["days"] == ["14"]
        assert fetch(store, opener, now=NOW + timedelta(hours=21)) is None  # not due
        assert fetch(store, opener, now=NOW + timedelta(hours=27)) == 5
        assert store.release_groups_seen(OTHER) == {}  # nobody follows them
    assert len(opener.requests) == 2


def test_nothing_asked_without_a_username(tmp_path: Path):
    opener = FakeOpener()
    with store_at(tmp_path) as store:
        assert fetch(store, opener, user=" ") is None
        assert personal.suggestions(store, OWNER_ID, "") == []
    assert opener.requests == []


def test_an_unknown_user_is_remembered_without_a_pause(tmp_path: Path):
    opener = FakeOpener(404)
    pause = ServicePause()
    with store_at(tmp_path) as store:
        assert fetch(store, opener, pause) == 0
        assert not pause.remaining()
        assert fetch(store, opener, pause, now=NOW + timedelta(hours=1)) is None
        assert personal.suggestions(store, OWNER_ID, USER) == []
    assert len(opener.requests) == 1


def test_an_error_pauses_listenbrainz(tmp_path: Path):
    opener = FakeOpener(500)
    pause = ServicePause()
    with store_at(tmp_path) as store:
        with pytest.raises(ServiceError):
            fetch(store, opener, pause)
        assert pause.remaining()
        with pytest.raises(ServiceError, match="paused"):
            fetch(store, opener, pause)
        assert personal.due(store, USER, now=NOW)
    assert len(opener.requests) == 1


def test_suggestions_skip_followed_and_dismissed_best_ranked_first(tmp_path: Path):
    with store_at(tmp_path) as store:
        store.follow(OWNER_ID, Follow(ARTIST, "Charli xcx"))
        fetch(store, FakeOpener(FEED), user="rob")  # the cache key ignores case
        hits = personal.suggestions(store, OWNER_ID, USER)
        # not RG_FRESH (followed), not Various Artists
        assert [h.mbid for h in hits] == [RG_DUO, RG_ELSE]
        assert hits[0].artist_names == ("Other One", "The Duo")  # read from the credit
        assert hits[1].first_release_date == "2026-10-27"  # the earliest edition

        store.dismiss(OWNER_ID, "release-group", RG_DUO)
        store.follow(OWNER_ID, Follow(OTHER, "Other One"))
        assert personal.suggestions(store, OWNER_ID, USER) == []
        assert personal.listed(store, USER, RG_DUO)
        assert not personal.listed(store, USER, "0a1b2c3d-0000-4000-8000-0000000000ff")


def test_credit_names():
    assert personal.credit_names("Simon & Garfunkel", 1) == ("Simon & Garfunkel",)
    assert personal.credit_names("A, B and C", 3) == ("A", "B", "C")
    assert personal.credit_names("A feat. B", 2) == ("A", "B")
    assert personal.credit_names("Iron & Wine & Ben Bridwell", 2) == ("", "")  # can't tell


def test_the_home_page_shows_them_with_follow(tmp_path: Path):
    settings = settings_for(tmp_path, listenbrainz_user=USER)
    with Store(settings.database_path) as store:
        store.save_cached(
            "listenbrainz", personal.CACHE_KIND, "rob",
            {"payload": {"releases": [release(RG_SOON, "Soon Enough", "2099-01-01", OTHER,
                                              artist_credit_name="Other One")]}},
            "2099-01-01T00:00:00+00:00",
        )  # fmt: skip
        store.save_cover(RG_SOON, b"jpeg", "image/jpeg", "2099-01-01T00:00:00+00:00")
    client = TestClient(create_app(settings))
    page = client.get("/").text
    assert "You might care about" in page and "Soon Enough" in page
    assert "out 2099-01-01" in page
    assert f'action="/artists/{OTHER}/follow"' in page
    assert client.get(f"/cover/{RG_SOON}").status_code == 200  # listed: its cover is served
    assert client.get(f"/cover/{RG_DUO}").status_code == 404  # not listed: not fetched

    page = TestClient(create_app(settings_for(tmp_path))).get("/").text
    assert "You might care about" not in page  # no username, no section


def test_the_worker_fetches_them_with_the_sitewide_feed(tmp_path: Path, monkeypatch):
    lb_opener = FakeOpener(
        {"payload": {"releases": []}}, FEED, {"playlists": []}, b"", {"payload": {"listens": []}}
    )
    monkeypatch.setattr(listenbrainz, "client_for", lambda settings: lb_client(lb_opener))
    monkeypatch.setattr(musicbrainz, "client_for", lambda settings: mb_client(FakeOpener()))
    with store_at(tmp_path) as store:
        store.follow(OWNER_ID, Follow(ARTIST, "Charli xcx"))
        store.save_cached(
            "musicbrainz", follows.CACHE_KIND, ARTIST, {}, "2099-01-01T00:00:00+00:00"
        )  # not due: no MusicBrainz request
    reconcile.Reconciler(settings_for(tmp_path, listenbrainz_user=USER)).run_once()
    assert [r.full_url.split("?")[0] for r in lb_opener.requests] == [
        "https://api.listenbrainz.org/1/explore/fresh-releases/",
        "https://api.listenbrainz.org/1/user/Rob/fresh_releases",
        "https://api.listenbrainz.org/1/user/Rob/playlists/createdfor",  # and its playlists
        "https://api.listenbrainz.org/1/stats/user/Rob/release-groups",  # listens.py (a 204)
        "https://api.listenbrainz.org/1/user/Rob/listens",  # history.py
    ]
