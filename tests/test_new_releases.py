from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from groovehound import follows, new_releases
from groovehound.db import OWNER_ID, Follow, LibraryRelease, Store
from groovehound.web import create_app

from .conftest import settings_for
from .test_follows import BROWSE, RG_OLD, group, store_at
from .test_search import ARTIST, RG, RG_COMP

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
FOLLOWED = "2026-10-01T12:00:00+00:00"
LATER = "2026-10-20T12:00:00+00:00"  # a re-browse after following
RG_NEW = "0a1b2c3d-0000-4000-8000-000000000010"
RG_SOON = "0a1b2c3d-0000-4000-8000-000000000011"
RG_ADDED = "0a1b2c3d-0000-4000-8000-000000000012"  # an old album MusicBrainz only just got
RG_LIVE = "0a1b2c3d-0000-4000-8000-000000000013"
RG_SINGLE = "0a1b2c3d-0000-4000-8000-000000000014"
RG_UNDATED = "0a1b2c3d-0000-4000-8000-000000000015"
RG_RECENT = "0a1b2c3d-0000-4000-8000-000000000016"  # out a month before NOW


def single(mbid: str, title: str, date: str) -> dict:
    return {**group(mbid, title, date), "primary-type": "Single"}


def followed(store: Store, **changes) -> None:
    """Charli followed at FOLLOWED, with BROWSE, RG_RECENT and RG_SOON (announced) as the
    baseline, and a re-browse at LATER that found more."""
    store.follow(OWNER_ID, Follow(ARTIST, "Charli xcx", followed_at=FOLLOWED))
    if changes.get("types"):
        store.set_follow_types(OWNER_ID, ARTIST, changes["types"], FOLLOWED)
    baseline = [
        *BROWSE["release-groups"],
        group(RG_RECENT, "Last Month", "2026-10-02"),
        group(RG_SOON, "Next One", "2027-02"),
    ]
    store.see_release_groups(ARTIST, (g["id"] for g in baseline), FOLLOWED)
    found = [
        group(RG_NEW, "New One", "2026-10-17"),
        group(RG_ADDED, "Demo Days", "2009"),
        group(RG_LIVE, "Live Somewhere", "2026-10-10", "Live"),
        group(RG_UNDATED, "Undated"),
    ]
    store.see_release_groups(ARTIST, (g["id"] for g in found), LATER)
    store.save_cached(
        "musicbrainz", follows.CACHE_KIND, ARTIST, {"release-groups": [*baseline, *found]},
        "2026-11-20T00:00:00+00:00",
    )  # fmt: skip
    singles = [single(RG_SINGLE, "A Single", "2026-10-18")]
    store.see_release_groups(ARTIST, [RG_SINGLE], LATER)
    store.save_cached(
        "musicbrainz", follows.SINGLES_KIND, ARTIST, {"release-groups": singles},
        "2026-11-20T00:00:00+00:00",
    )  # fmt: skip


def mbids(releases) -> list[str]:
    return [r.hit.mbid for r in releases]


def test_new_and_upcoming_from_the_cache(tmp_path: Path):
    with store_at(tmp_path) as store:
        followed(store)
        feed = new_releases.feed(store, OWNER_ID, now=NOW)
        assert mbids(feed.upcoming) == [RG_SOON]  # announced before following: still shown
        # out lately, though out before following (RG_RECENT); not older things out then (RG,
        # RG_OLD), an old album only just added, live (not followed for it), or singles (not
        # followed for them, though cached)
        assert mbids(feed.new) == [RG_NEW, RG_RECENT, RG_UNDATED]
        assert feed.new[0].artist.name == "Charli xcx"
        assert feed.new[0].first_seen == LATER


def test_ones_in_the_library_come_last(tmp_path: Path):
    with store_at(tmp_path) as store:
        followed(store)
        store.replace_library(
            OWNER_ID, [LibraryRelease(RG_NEW + "-r", RG_NEW, (), "New One", "", "", 1)], LATER
        )
        feed = new_releases.feed(store, OWNER_ID, now=NOW)
        assert mbids(feed.new) == [RG_RECENT, RG_UNDATED, RG_NEW]


def test_new_for_a_while_then_not(tmp_path: Path):
    with store_at(tmp_path) as store:
        followed(store)
        later = datetime.fromisoformat(LATER) + new_releases.NEW_FOR + timedelta(seconds=1)
        feed = new_releases.feed(store, OWNER_ID, now=later)
        assert mbids(feed.new) == []
        # once out, an upcoming release is new for a while: "2027-02" could be the 28th
        feed = new_releases.feed(store, OWNER_ID, now=datetime(2027, 3, 1, tzinfo=UTC))
        assert (mbids(feed.upcoming), mbids(feed.new)) == ([], [RG_SOON])
        feed = new_releases.feed(store, OWNER_ID, now=datetime(2027, 6, 1, tzinfo=UTC))
        assert not feed


def test_the_follows_types_decide(tmp_path: Path):
    with store_at(tmp_path) as store:
        followed(store, types=("album", "single", "live"))
        feed = new_releases.feed(store, OWNER_ID, now=NOW)
        assert mbids(feed.new) == [RG_SINGLE, RG_NEW, RG_LIVE, RG_RECENT, RG_UNDATED]


def test_dismissed_stays_dismissed(tmp_path: Path):
    with store_at(tmp_path) as store:
        followed(store)
        assert store.dismiss(OWNER_ID, new_releases.DISMISSED_KIND, RG_NEW)
        assert not store.dismiss(OWNER_ID, new_releases.DISMISSED_KIND, RG_NEW)
        store.dismiss(OWNER_ID, new_releases.DISMISSED_KIND, RG_SOON)
        feed = new_releases.feed(store, OWNER_ID, now=NOW)
        assert (mbids(feed.upcoming), mbids(feed.new)) == ([], [RG_RECENT, RG_UNDATED])


def test_nothing_without_follows(tmp_path: Path):
    with store_at(tmp_path) as store:
        store.see_release_groups(ARTIST, [RG_NEW], LATER)
        assert not new_releases.feed(store, OWNER_ID, now=NOW)


def test_home_page_lists_them_and_dismiss_removes_one(tmp_path: Path):
    settings = settings_for(tmp_path)
    client = TestClient(create_app(settings))
    now = datetime.now(UTC)
    with Store(settings.database_path) as store:
        store.follow(OWNER_ID, Follow(ARTIST, "Charli xcx", followed_at=_iso(now - timedelta(30))))
        store.see_release_groups(ARTIST, [RG], _iso(now - timedelta(30)))
        store.see_release_groups(ARTIST, [RG_NEW], _iso(now - timedelta(2)))
        store.save_cached(
            "musicbrainz", follows.CACHE_KIND, ARTIST,
            {"release-groups": [group(RG, "Brat", "2024-06-07"),
                                group(RG_NEW, "New One", (now - timedelta(3)).date().isoformat())]},
            _iso(now + timedelta(7)),
        )  # fmt: skip
    page = client.get("/").text
    assert "New One" in page and "Brat" not in page
    assert f'href="/artists/{ARTIST}"' in page and f"/cover/{RG_NEW}" in page
    assert 'From the <a href="/artists">1 artist you follow</a>' in " ".join(page.split())
    assert "In your library" not in page
    with Store(settings.database_path) as store:
        store.replace_library(
            OWNER_ID, [LibraryRelease(RG_NEW + "-r", RG_NEW, (), "New One", "", "", 1)], _iso(now)
        )
    assert "✓ In your library" in client.get("/").text

    # without JavaScript: back to the page; with HTMX: an empty swap
    response = client.post(f"/new/{RG_NEW}/dismiss", data={"next": "/"}, follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/")
    assert client.post(f"/new/{RG_NEW}/dismiss", headers={"HX-Request": "true"}).text == ""
    page = client.get("/").text
    assert "New One" not in page and "Nothing new yet" in page
    assert client.post("/new/not-an-mbid/dismiss").status_code == 404


def test_covers_of_followed_artists_release_groups_only(tmp_path: Path):
    with store_at(tmp_path) as store:
        store.see_release_groups(ARTIST, [RG_NEW, RG_COMP])
        assert not store.followed_release_group(OWNER_ID, RG_NEW)
        store.follow(OWNER_ID, Follow(ARTIST, "Charli xcx"))
        assert store.followed_release_group(OWNER_ID, RG_NEW)
        assert not store.followed_release_group(OWNER_ID, RG_OLD)


def _iso(when: datetime) -> str:
    return when.isoformat(timespec="seconds")


def partial(store: Store, followed_at: str, seen: str, date: str) -> None:
    """Charli followed at `followed_at`, with one album dated `date`, first seen at `seen`."""
    store.follow(OWNER_ID, Follow(ARTIST, "Charli xcx", followed_at=followed_at))
    store.see_release_groups(ARTIST, [RG_NEW], seen)
    store.save_cached(
        "musicbrainz", follows.CACHE_KIND, ARTIST,
        {"release-groups": [group(RG_NEW, "New One", date)]}, "2027-12-01T00:00:00+00:00",
    )  # fmt: skip


@pytest.mark.parametrize(
    ("followed_at", "seen", "date", "now", "shown"),
    [
        # a year alone: new for NEW_FOR from when a re-browse first saw it, not all year
        ("2026-01-05", "2026-02-10", "2026", "2026-05-10", "new"),
        ("2026-01-05", "2026-02-10", "2026", "2026-05-12", ""),
        # out before following: counted from following (it can't be told from January's)
        ("2026-10-25", "2026-10-25", "2026", "2026-11-01", "new"),
        # announced: not new before its year or month starts, then for NEW_FOR from then
        ("2026-11-01", "2026-11-01", "2027", "2026-11-01", "upcoming"),
        ("2026-11-01", "2026-11-01", "2027", "2027-03-31", "new"),
        ("2026-11-01", "2026-11-01", "2027", "2027-04-02", ""),
        ("2026-01-05", "2026-01-05", "2026-10", "2026-11-01", "new"),
        # never later than the end of the year or month it names (when out before following;
        # first seen after it, it's new as something MusicBrainz got late)
        ("2026-10-20", "2026-10-20", "2025", "2026-11-01", ""),
        ("2026-01-05", "2026-10-20", "2025", "2026-11-01", "new"),
    ],
)
def test_partial_dates_count_from_first_seen(tmp_path: Path, followed_at, seen, date, now, shown):
    with store_at(tmp_path) as store:
        partial(store, f"{followed_at}T12:00:00+00:00", f"{seen}T12:00:00+00:00", date)
        feed = new_releases.feed(
            store, OWNER_ID, now=datetime.fromisoformat(f"{now}T12:00:00+00:00")
        )
        found = "upcoming" if feed.upcoming else "new" if feed.new else ""
        assert found == shown


def test_a_full_date_from_a_refresh_wins(tmp_path: Path):
    with store_at(tmp_path) as store:
        # out before following: only the date says it's new
        partial(store, "2026-10-20T12:00:00+00:00", "2026-10-20T12:00:00+00:00", "2026")
        assert new_releases.feed(store, OWNER_ID, now=NOW).new
        store.save_cached(
            "musicbrainz", follows.CACHE_KIND, ARTIST,
            {"release-groups": [group(RG_NEW, "New One", "2026-02-03")]},
            "2027-12-01T00:00:00+00:00",
        )  # fmt: skip
        assert not new_releases.feed(store, OWNER_ID, now=NOW)
