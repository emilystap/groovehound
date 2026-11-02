from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from groovehound import follows, fresh, new_releases
from groovehound.brainz.http import ServiceError, ServicePause
from groovehound.db import OWNER_ID, Follow, Store
from groovehound.web import create_app

from .conftest import FakeOpener, settings_for
from .test_brainz import lb_client, params
from .test_follows import group, store_at
from .test_search import ARTIST, RG

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
FOLLOWED = "2026-09-01T12:00:00+00:00"
OTHER = "0a1b2c3d-0000-4000-8000-0000000000aa"  # an artist nobody follows
RG_FRESH = "0a1b2c3d-0000-4000-8000-000000000020"
RG_SOON = "0a1b2c3d-0000-4000-8000-000000000021"
RG_LIVE = "0a1b2c3d-0000-4000-8000-000000000022"
RG_ELSE = "0a1b2c3d-0000-4000-8000-000000000023"


def release(group_mbid: str, name: str, date: str, *artists: str, **extra) -> dict:
    """One release in the feed, as ListenBrainz lists it."""
    return {
        "artist_credit_name": "Charli xcx",
        "artist_mbids": list(artists or (ARTIST,)),
        "release_date": date,
        "release_group_mbid": group_mbid,
        "release_group_primary_type": "Album",
        "release_mbid": f"{group_mbid[:-4]}ffff",
        "release_name": name,
        **extra,
    }


FEED = {
    "payload": {
        "releases": [
            release(RG_FRESH, "Fresh One", "2026-10-30"),
            release(RG_FRESH, "Fresh One", "2026-10-28"),  # another edition, earlier
            release(RG_SOON, "Soon", "2026-11-10"),
            release(RG_LIVE, "Live Now", "2026-10-25", release_group_secondary_type="Live"),
            release(RG, "Brat (reissue)", "2026-10-29"),  # the browse knows it's from 2024
            release(RG_ELSE, "Someone Else", "2026-10-30", OTHER),
        ],
        "total_count": 6,
    }
}


def followed(store: Store) -> None:
    store.follow(OWNER_ID, Follow(ARTIST, "Charli xcx", followed_at=FOLLOWED))
    store.see_release_groups(ARTIST, [RG], FOLLOWED)
    store.save_cached(
        "musicbrainz", follows.CACHE_KIND, ARTIST,
        {"release-groups": [group(RG, "Brat", "2024-06-07")]}, "2026-11-20T00:00:00+00:00",
    )  # fmt: skip


def fetch(store: Store, opener: FakeOpener, pause: ServicePause | None = None, now=NOW):
    return fresh.fetch(store, lb_client(opener), pause or ServicePause(), now=now)


def test_one_request_a_day_filtered_by_followed_artists(tmp_path: Path):
    opener = FakeOpener(FEED, FEED)
    with store_at(tmp_path) as store:
        followed(store)
        assert fetch(store, opener) == 3  # RG_FRESH, RG_SOON, RG_LIVE; RG was seen already
        sent = params(opener.requests[0])
        assert opener.requests[0].full_url.startswith(
            "https://api.listenbrainz.org/1/explore/fresh-releases/"
        )
        assert (sent["days"], sent["release_date"]) == (["14"], ["2026-11-01"])
        seen = store.release_groups_seen(ARTIST)
        assert seen[RG_FRESH] == NOW.isoformat() and seen[RG] == FOLLOWED
        assert store.release_groups_seen(OTHER) == {}  # nobody follows them
        entry = store.cached("listenbrainz", fresh.CACHE_KIND, fresh.CACHE_KEY)
        assert NOW + timedelta(hours=22) <= datetime.fromisoformat(entry.refresh_after)
        assert datetime.fromisoformat(entry.refresh_after) <= NOW + timedelta(hours=26)

        assert fetch(store, opener, now=NOW + timedelta(hours=21)) is None  # not due
        assert len(opener.requests) == 1
        assert fetch(store, opener, now=NOW + timedelta(hours=27)) == 0  # due: nothing new
        assert len(opener.requests) == 2


def test_nothing_asked_while_nobody_follows_anyone(tmp_path: Path):
    opener = FakeOpener()
    with store_at(tmp_path) as store:
        assert fetch(store, opener) is None
    assert opener.requests == []


def test_an_error_pauses_listenbrainz(tmp_path: Path):
    opener = FakeOpener(500)
    pause = ServicePause()
    with store_at(tmp_path) as store:
        followed(store)
        with pytest.raises(ServiceError):
            fetch(store, opener, pause)
        assert pause.remaining()
        with pytest.raises(ServiceError, match="paused"):
            fetch(store, opener, pause)  # asks nothing
        assert fresh.due(store, now=NOW)
    assert len(opener.requests) == 1


def test_the_home_feed_shows_what_the_browse_doesnt_have_yet(tmp_path: Path):
    opener = FakeOpener(FEED)
    with store_at(tmp_path) as store:
        followed(store)
        fetch(store, opener)
        feed = new_releases.feed(store, OWNER_ID, now=NOW)
        assert [r.hit.mbid for r in feed.upcoming] == [RG_SOON]
        # not RG: the browse's date (2024) wins over the reissue's; not live (not followed
        # for it); not someone else's
        assert [r.hit.mbid for r in feed.new] == [RG_FRESH]
        hit = feed.new[0].hit
        assert (hit.title, hit.first_release_date, hit.artist_names) == (
            "Fresh One",
            "2026-10-28",  # the earliest edition
            ("Charli xcx",),
        )
        assert feed.new[0].first_seen == NOW.isoformat()

        # once a browse has it, the browse's view is shown
        store.save_cached(
            "musicbrainz", follows.CACHE_KIND, ARTIST,
            {"release-groups": [group(RG, "Brat", "2024-06-07"),
                                group(RG_FRESH, "Fresh One (MB)", "2026-10-28")]},
            "2026-11-20T00:00:00+00:00",
        )  # fmt: skip
        feed = new_releases.feed(store, OWNER_ID, now=NOW)
        assert [r.hit.title for r in feed.new] == ["Fresh One (MB)"]


def test_releases_reads_the_payload_or_a_bare_list():
    assert fresh.releases({"payload": {"releases": [{"a": 1}, "junk"]}}) == [{"a": 1}]
    assert fresh.releases([{"a": 1}]) == [{"a": 1}]
    assert fresh.releases({}) == []


def test_links_to_other_sites_open_in_a_new_tab(tmp_path: Path):
    settings = settings_for(tmp_path)
    with Store(settings.database_path) as store:
        followed(store)
        store.save_cached(
            "listenbrainz", fresh.CACHE_KIND, fresh.CACHE_KEY,
            {"payload": {"releases": [release(RG_FRESH, "Fresh One",
                                              datetime.now(UTC).date().isoformat())]}},
            "2099-01-01T00:00:00+00:00",
        )  # fmt: skip
    page = TestClient(create_app(settings)).get("/").text
    assert "Fresh One" in page
    links = [a for a in page.split("<a ")[1:] if 'href="http' in a.split(">")[0]]
    assert links and all('target="_blank"' in a.split(">")[0] for a in links)
    assert 'href="/artists" target' not in page  # the app's own links stay in the tab
