import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from groovehound import follows
from groovehound.brainz.http import NotFound, ServiceError, ServicePause
from groovehound.db import OWNER_ID, Store, WishlistItem
from groovehound.web import create_app

from .conftest import FakeOpener, settings_for
from .test_app import search_app
from .test_brainz import mb_client, params
from .test_search import ARTIST, GUEST, RELEASE_GROUPS, RG, RG_COMP

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
VARIOUS = "89ad4ac3-39f7-470e-963a-56509c546377"
RG_OLD = "0a1b2c3d-0000-4000-8000-000000000001"
RG_EP = "0a1b2c3d-0000-4000-8000-000000000002"
CHARLI = {
    "id": ARTIST,
    "name": "Charli xcx",
    "sort-name": "Charli xcx",
    "disambiguation": "English singer",
    "type": "Person",
}


def group(mbid: str, title: str, date: str = "", *secondary: str, credit=None) -> dict:
    return {
        "id": mbid,
        "title": title,
        "primary-type": "Album",
        "secondary-types": list(secondary),
        "first-release-date": date,
        "artist-credit": credit or [{"name": "Charli XCX", "artist": CHARLI}],
    }


BROWSE = {
    "release-group-count": 4,
    "release-groups": [
        group(RG_OLD, "True Romance", "2013-04-12"),
        group(RG, "Brat", "2024-06-07"),
        group(RG_COMP, "Live at Glastonbury", "2024-07", "Live"),
        group(RG_EP, "Undated EP"),
    ],
}


def store_at(tmp_path: Path) -> Store:
    return Store(tmp_path / "groovehound.db")


def test_credited_names_each_followable_artist():
    assert follows.credited((ARTIST, GUEST), ("Charli xcx", "Guest")) == [
        follows.Credited(ARTIST, "Charli xcx"),
        follows.Credited(GUEST, "Guest"),
    ]
    # saved before 2a: no names; one artist is named by the credit, several are numbered
    assert follows.credited((ARTIST,), (), "Charli xcx") == [follows.Credited(ARTIST, "Charli xcx")]
    assert [c.name for c in follows.credited((ARTIST, GUEST), (), "A & B")] == [
        "artist 1",
        "artist 2",
    ]
    assert follows.credited((VARIOUS, ARTIST), ("Various Artists", "Charli xcx")) == [
        follows.Credited(ARTIST, "Charli xcx")
    ]


def test_following_browses_once_and_records_what_is_out(tmp_path: Path):
    opener = FakeOpener(BROWSE)
    with store_at(tmp_path) as store:
        pause = ServicePause()
        follow = follows.follow(store, mb_client(opener), pause, OWNER_ID, ARTIST, now=NOW)
        assert (follow.name, follow.disambiguation) == ("Charli xcx", "English singer")
        assert follow.followed_at == "2026-11-01T12:00:00+00:00"
        (request,) = opener.requests
        assert params(request)["artist"] == [ARTIST]
        assert params(request)["type"] == ["album|ep"]
        seen = store.release_groups_seen(ARTIST)
        assert set(seen) == {RG_OLD, RG, RG_COMP, RG_EP}
        assert set(seen.values()) == {"2026-11-01T12:00:00+00:00"}

        # already following, or following again while the browse is cached: no requests
        follows.follow(store, mb_client(opener), pause, OWNER_ID, ARTIST, now=NOW)
        assert store.unfollow(OWNER_ID, ARTIST)
        later = NOW + timedelta(days=3)
        again = follows.follow(store, mb_client(opener), pause, OWNER_ID, ARTIST, now=later)
        assert again.followed_at == later.isoformat()
        assert len(opener.requests) == 1
        assert store.followed(OWNER_ID, [ARTIST, GUEST]) == {ARTIST}


def test_a_refresh_keeps_when_release_groups_were_first_seen(tmp_path: Path):
    newer = {**BROWSE, "release-groups": [*BROWSE["release-groups"], group(GUEST, "New one")]}
    opener = FakeOpener(BROWSE, newer)
    with store_at(tmp_path) as store:
        client, pause = mb_client(opener), ServicePause()
        follows.discography(store, client, pause, ARTIST, now=NOW)
        later = NOW + follows.TTL[1] + timedelta(days=1)
        follows.discography(store, client, pause, ARTIST, now=later)
        seen = store.release_groups_seen(ARTIST)
    assert seen[RG] == NOW.isoformat()
    assert seen[GUEST] == later.isoformat()


def test_discography_newest_first_with_live_and_compilations_apart(tmp_path: Path):
    truncated = {**BROWSE, "release-group-count": 450}
    with store_at(tmp_path) as store:
        found = follows.discography(
            store,
            mb_client(FakeOpener(truncated, {**truncated, "release-groups": []})),
            ServicePause(),
            ARTIST,
            now=NOW,
        )
        assert [h.title for h in found.main] == ["Brat", "True Romance", "Undated EP"]
        assert [h.title for h in found.other] == ["Live at Glastonbury"]
        assert found.count == 450
        cached, due = follows.cached_discography(store, ARTIST, now=NOW)
        assert cached == found and not due
        _, due = follows.cached_discography(store, ARTIST, now=NOW + follows.TTL[1])
        assert due


def test_cached_browses_are_refreshed_at_random_within_the_ttl(tmp_path: Path):
    with store_at(tmp_path) as store:
        follows.discography(
            store, mb_client(FakeOpener(BROWSE)), ServicePause(), ARTIST, now=NOW,
            rng=random.Random(1),
        )  # fmt: skip
        entry = store.cached("musicbrainz", follows.CACHE_KIND, ARTIST)
    after = datetime.fromisoformat(entry.refresh_after) - NOW
    assert follows.TTL[0] <= after <= follows.TTL[1]


def test_an_artist_without_albums_is_named_by_the_lookup(tmp_path: Path):
    empty = {"release-group-count": 0, "release-groups": []}
    opener = FakeOpener(empty, CHARLI)
    with store_at(tmp_path) as store:
        found = follows.discography(store, mb_client(opener), ServicePause(), ARTIST, now=NOW)
        assert found.artist.name == "Charli xcx" and found.release_groups == ()
        assert opener.requests[1].full_url.split("?")[0].endswith(f"/artist/{ARTIST}")
        follows.discography(store, mb_client(opener), ServicePause(), ARTIST, now=NOW)
    assert len(opener.requests) == 2  # both cached


def test_an_unknown_artist_is_remembered(tmp_path: Path):
    opener = FakeOpener(404, 404)  # the browse finds nothing, the lookup neither
    with store_at(tmp_path) as store:
        for _ in range(2):
            with pytest.raises(NotFound):
                follows.follow(store, mb_client(opener), ServicePause(), OWNER_ID, ARTIST, now=NOW)
        assert store.follows(OWNER_ID) == []
    assert len(opener.requests) == 2  # the second try asked nothing


def test_special_purpose_artists_cannot_be_followed(tmp_path: Path):
    opener = FakeOpener()
    with store_at(tmp_path) as store, pytest.raises(NotFound):
        follows.follow(store, mb_client(opener), ServicePause(), OWNER_ID, VARIOUS, now=NOW)
    assert opener.requests == []


def test_a_failed_browse_follows_no_one_and_pauses(tmp_path: Path):
    opener = FakeOpener(500, 500, 500)
    pause = ServicePause()
    with store_at(tmp_path) as store:
        with pytest.raises(ServiceError):
            follows.follow(store, mb_client(opener), pause, OWNER_ID, ARTIST, now=NOW)
        asked = len(opener.requests)
        with pytest.raises(ServiceError, match="paused"):
            follows.follow(store, mb_client(opener), pause, OWNER_ID, ARTIST, now=NOW)
        assert store.follows(OWNER_ID) == []
    assert len(opener.requests) == asked  # paused: not asked again


# --- Web -------------------------------------------------------------------------------------


def test_artist_page_then_follow_costs_one_browse(tmp_path: Path, monkeypatch):
    opener = search_app(tmp_path, monkeypatch, BROWSE)
    client = TestClient(create_app(settings_for(tmp_path)))
    page = client.get(f"/artists/{ARTIST.upper()}").text
    assert "Charli xcx" in page and "English singer" in page
    assert page.index("Brat") < page.index("True Romance")
    assert "Live at Glastonbury" in page and page.count("Add to wishlist") == 4
    assert f'action="/artists/{ARTIST}/follow"' in page

    followed = client.post(
        f"/artists/{ARTIST}/follow",
        data={"name": "Charli xcx", "next": f"/artists/{ARTIST}"},
        headers={"HX-Request": "true"},
    )
    assert f'hx-swap-oob="outerHTML:.follow-{ARTIST}"' in followed.text
    assert "Following" in followed.text
    assert len(opener.requests) == 1  # the page's browse was cached

    assert "Charli xcx" in client.get("/artists").text
    assert "You follow" in client.get("/").text
    page = client.get(f"/artists/{ARTIST}").text
    assert f'action="/artists/{ARTIST}/unfollow"' in page

    gone = client.post(
        f"/artists/{ARTIST}/unfollow",
        data={"name": "Charli xcx"},
        headers={"HX-Request": "true"},
    )
    assert f'action="/artists/{ARTIST}/follow"' in gone.text and "Charli xcx" in gone.text
    assert "don't follow anyone" in client.get("/artists").text


def test_follow_without_javascript_goes_back(tmp_path: Path, monkeypatch):
    search_app(tmp_path, monkeypatch, BROWSE)
    client = TestClient(create_app(settings_for(tmp_path)), follow_redirects=False)
    back = client.post(f"/artists/{ARTIST}/follow", data={"next": "/search?title=brat"})
    assert (back.status_code, back.headers["location"]) == (303, "/search?title=brat")
    back = client.post(f"/artists/{ARTIST}/follow", data={"next": "https://evil.example"})
    assert back.headers["location"] == "/"


def test_follow_errors_are_shown_on_the_button(tmp_path: Path, monkeypatch):
    search_app(tmp_path, monkeypatch, 500, 500, 500)
    client = TestClient(create_app(settings_for(tmp_path)))
    response = client.post(
        f"/artists/{ARTIST}/follow", data={"name": "Charli xcx"}, headers={"HX-Request": "true"}
    )
    assert "follow-error" in response.text and "Follow" in response.text
    assert client.get("/artists").text.count("follow-") == 0


def test_artist_routes_reject_what_is_not_an_artist(tmp_path: Path, monkeypatch):
    opener = search_app(tmp_path, monkeypatch, 404, 404)
    client = TestClient(create_app(settings_for(tmp_path)))
    assert client.get("/artists/not-an-mbid").status_code == 404
    assert client.post("/artists/not-an-mbid/follow").status_code == 404
    assert client.post(f"/artists/{VARIOUS}/follow").status_code == 404
    assert client.get(f"/artists/{ARTIST}").status_code == 404  # MusicBrainz has no such artist
    assert len(opener.requests) == 2


def test_without_a_contact_nothing_is_asked(tmp_path: Path, monkeypatch):
    opener = search_app(tmp_path, monkeypatch)
    from groovehound.config import Settings

    client = TestClient(create_app(Settings(database_path=tmp_path / "groovehound.db")))
    assert "Not connected" in client.get(f"/artists/{ARTIST}").text
    response = client.post(f"/artists/{ARTIST}/follow", headers={"HX-Request": "true"})
    assert "follow-error" in response.text
    assert opener.requests == []


def test_search_results_and_wishlist_items_have_follow_buttons(tmp_path: Path, monkeypatch):
    opener = search_app(tmp_path, monkeypatch, RELEASE_GROUPS, BROWSE)
    client = TestClient(create_app(settings_for(tmp_path)))
    page = client.get("/search", params={"title": "brat"}).text
    assert f"follow-{ARTIST}" in page and f"follow-{GUEST}" in page
    assert f'href="/artists/{GUEST}">Guest</a>' in page
    # the names go with the result, as JSON (a name can have a comma)
    assert f"value='{json.dumps(['Charli xcx', 'Guest'])}'" in page

    client.post(
        "/wishlist",
        data={
            "release_group": RG,
            "title": "Brat",
            "artist_credit": "Charli xcx feat. Guest",
            "artist_mbids": f"{ARTIST},{GUEST}",
            "artist_names": json.dumps(["Charli xcx", "Crosby, Stills & Nash"]),
        },
    )
    client.post(f"/artists/{ARTIST}/follow")
    wishlist = client.get("/wishlist").text
    assert f'action="/artists/{ARTIST}/unfollow"' in wishlist
    assert f'action="/artists/{GUEST}/follow"' in wishlist
    assert ">Crosby, Stills &amp; Nash</a>" in wishlist
    assert len(opener.requests) == 2

    with store_at(tmp_path) as store:
        (item,) = store.wishlist(OWNER_ID)
    assert item.artist_names == ("Charli xcx", "Crosby, Stills & Nash")


def test_names_that_do_not_match_the_artists_are_dropped(tmp_path: Path):
    client = TestClient(create_app(settings_for(tmp_path)))
    for names in ("not json", json.dumps(["one", "too many"]), json.dumps({"a": 1})):
        client.post(
            "/wishlist",
            data={"release_group": RG, "title": "Brat", "artist_mbids": ARTIST,
                  "artist_names": names},
        )  # fmt: skip
        with store_at(tmp_path) as store:
            (item,) = store.wishlist(OWNER_ID)
            assert item.artist_names == ()
            store.remove_from_wishlist(OWNER_ID, item.id)


def test_items_saved_before_names_still_render(tmp_path: Path):
    with store_at(tmp_path) as store:
        store.add_to_wishlist(
            OWNER_ID, WishlistItem(RG, "Brat", "Charli xcx", artist_mbids=(ARTIST,))
        )
    page = TestClient(create_app(settings_for(tmp_path))).get("/wishlist").text
    assert f'href="/artists/{ARTIST}">Charli xcx</a>' in page
