"""The album page (docs/ui-roadmap.md, "Album pages", phase U2): album.py and /albums/<mbid>."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from groovehound import album, releases, tracks
from groovehound.brainz import coverart, musicbrainz
from groovehound.brainz.http import ServicePause
from groovehound.config import Settings
from groovehound.db import OWNER_ID, Store, WishlistItem
from groovehound.web import create_app

from .conftest import FakeOpener, settings_for
from .test_brainz import mb_client, params
from .test_covers import caa_client
from .test_tracks import BROWSE, RELEASE, RG, TWO_DISCS, R, own_tagged
from .test_tracks import own as own_files

ARTIST = "cccccccc-cccc-cccc-cccc-cccccccccccc"
GROUP = {
    "id": RG,
    "title": "Album",
    "primary-type": "Album",
    "secondary-types": [],
    "first-release-date": "2020-03-01",
    "artist-credit": [
        {"name": "Artist", "joinphrase": "", "artist": {"id": ARTIST, "name": "Artist"}}
    ],
}
# The editions browse as it's asked for now: each release with its release group
NAMED = {"releases": [{**r, "release-group": GROUP} for r in BROWSE["releases"]]}
LATER = "2027-01-01T00:00:00+00:00"


def own(store: Store, release: str, rg: str, files: int, *recordings: str) -> None:
    """In the library since 2026-10-01."""
    own_files(store, release, rg, files, *recordings)
    with store.conn:
        store.conn.execute("UPDATE library SET added_at = '2026-10-01T00:00:00+00:00'")


def store_at(tmp_path: Path) -> Store:
    return Store(tmp_path / "groovehound.db")


def cache_browse(store: Store, data: dict = NAMED) -> None:
    store.save_cached("musicbrainz", releases.CACHE_KIND, RG, data, LATER)


def cache_tracks(store: Store, release: str = R[1], data: dict = RELEASE) -> None:
    store.save_cached("musicbrainz", tracks.CACHE_KIND, release, {**data, "id": release}, LATER)


def wish(store: Store, release: str | None = None) -> WishlistItem:
    store.add_to_wishlist(
        OWNER_ID,
        WishlistItem(
            RG, "Wished", "Wished Artist", (ARTIST,), ("Wished Artist",), "EP", release_mbid=release
        ),
    )
    return store.wishlist_item_of(OWNER_ID, RG)


# --- The header from what's stored ----------------------------------------------------------


def test_nothing_stored_knows_the_album(tmp_path: Path):
    with store_at(tmp_path) as store:
        assert album.album(store, OWNER_ID, "", RG) is None


def test_header_from_the_wishlist_first(tmp_path: Path):
    with store_at(tmp_path) as store:
        cache_browse(store)
        item = wish(store)
        found = album.album(store, OWNER_ID, "", RG)
    assert found.item == item
    assert (found.hit.title, found.hit.primary_type) == ("Wished", "EP")


def test_header_from_the_editions_browse(tmp_path: Path):
    with store_at(tmp_path) as store:
        cache_browse(store)
        found = album.album(store, OWNER_ID, "", RG)
    assert (found.hit.title, found.hit.artist_mbids, found.hit.first_release_date) == (
        "Album",
        (ARTIST,),
        "2020-03-01",
    )
    assert found.item is None and found.copy is None


def test_a_browse_from_before_names_no_release_group(tmp_path: Path):
    with store_at(tmp_path) as store:
        cache_browse(store, BROWSE)
        assert album.album(store, OWNER_ID, "", RG) is None


def test_header_from_an_artist_browse(tmp_path: Path):
    with store_at(tmp_path) as store:
        store.save_cached(
            "musicbrainz", "artist-release-groups", ARTIST, {"release-groups": [GROUP]}, LATER
        )
        store.see_release_groups(ARTIST, [RG])
        assert album.album(store, OWNER_ID, "", RG).hit.title == "Album"


def test_header_from_the_library_last(tmp_path: Path):
    with store_at(tmp_path) as store:
        own(store, R[1], RG, 2)
        found = album.album(store, OWNER_ID, "", RG)
    assert (found.hit.title, found.hit.artist_credit) == ("Album", "Artist")
    assert found.copy.editions[0].release_mbid == R[1] and found.copy.partial is None


def test_your_copy_counts_tracks_once_the_browse_is_cached(tmp_path: Path):
    with store_at(tmp_path) as store:
        own(store, R[1], RG, 8)
        cache_browse(store)
        copy = album.album(store, OWNER_ID, "", RG).copy
    assert (copy.partial.have, copy.partial.of) == (8, 12)
    assert copy.listens == 0 and copy.last_played == ""


# --- The track list -------------------------------------------------------------------------


def test_track_list_needs_the_browse_for_the_default_edition(tmp_path: Path):
    with store_at(tmp_path) as store:
        listing = album.track_list(store, OWNER_ID, RG)
        assert (listing.pick, listing.due) == (None, True)
        cache_browse(store)
        listing = album.track_list(store, OWNER_ID, RG)
        assert listing.pick == tracks.Pick(R[1], "default") and listing.due
        assert listing.details == "2020-03-01 · Digital Media · 12 tracks"
        cache_tracks(store)
        listing = album.track_list(store, OWNER_ID, RG)
    assert not listing.due and listing.tracks.total == 3


def test_the_library_edition_needs_no_browse(tmp_path: Path):
    with store_at(tmp_path) as store:
        own(store, R[3], RG, 1, "3333")
        cache_tracks(store, R[3])
        listing = album.track_list(store, OWNER_ID, RG)
    assert listing.pick == tracks.Pick(R[3], "library") and not listing.due
    assert listing.details == "2×CD · 3 tracks"  # noqa: RUF001  (from the track list)


def test_an_edition_asked_for_must_be_known(tmp_path: Path):
    with store_at(tmp_path) as store:
        cache_browse(store)
        item = wish(store, R[0])
        assert album.track_list(store, OWNER_ID, RG, item).pick == tracks.Pick(R[0], "wishlist")
        chosen = album.track_list(store, OWNER_ID, RG, item, R[3]).pick
        assert chosen == tracks.Pick(R[3], "chosen")
        unknown = "99999999-9999-9999-9999-999999999999"
        assert album.track_list(store, OWNER_ID, RG, item, unknown).pick.reason == "wishlist"


def test_fetch_track_list_makes_at_most_two_requests(tmp_path: Path):
    opener = FakeOpener(NAMED, RELEASE)
    with store_at(tmp_path) as store:
        listing = album.fetch_track_list(store, mb_client(opener), ServicePause(), OWNER_ID, RG)
        assert listing.tracks.total == 3 and not listing.due
        again = album.fetch_track_list(store, mb_client(opener), ServicePause(), OWNER_ID, RG)
    assert again == listing
    browse, lookup = opener.requests
    assert params(browse)["release-group"] == [RG]
    assert lookup.full_url.split("?")[0].endswith(f"/release/{R[1]}")


def test_tracks_length(tmp_path: Path):
    with store_at(tmp_path) as store:
        timed = tracks.track_list(store, OWNER_ID, RELEASE)
    assert timed.length_ms is None  # not every track's is known
    whole = {
        **RELEASE,
        "media": [RELEASE["media"][0] | {"tracks": RELEASE["media"][0]["tracks"][:1]}],
    }
    with store_at(tmp_path) as store:
        assert tracks.track_list(store, OWNER_ID, whole).length_ms == 200000


# --- The page --------------------------------------------------------------------------------


def album_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *answers) -> FakeOpener:
    opener = FakeOpener(*answers)
    monkeypatch.setattr(musicbrainz, "client_for", lambda settings: mb_client(opener))
    return opener


def test_page_from_the_cache_makes_no_request(tmp_path: Path, monkeypatch):
    opener = album_app(tmp_path, monkeypatch)
    with store_at(tmp_path) as store:
        cache_browse(store)
        cache_tracks(store)
    page = TestClient(create_app(settings_for(tmp_path))).get(f"/albums/{RG}").text
    assert "<h1>Album</h1>" in page and "Add to wishlist" in page
    assert "Song" in page and "3:20" in page and "Artist feat. Guest" in page
    assert (
        f'href="{musicbrainz.WEB_ROOT}/release-group/{RG}" rel="noopener" target="_blank"' in page
    )
    assert 'hx-trigger="load"' not in page  # nothing left to load
    assert "Cover Art Archive" in page  # the attribution footer
    assert opener.requests == []


def test_unknown_album_asks_for_its_editions_then_loads_its_tracks(tmp_path: Path, monkeypatch):
    opener = album_app(tmp_path, monkeypatch, NAMED, RELEASE)
    client = TestClient(create_app(settings_for(tmp_path)))
    page = client.get(f"/albums/{RG.upper()}").text
    assert "<h1>Album</h1>" in page and len(opener.requests) == 1
    assert f'hx-get="/albums/{RG}/tracks"' in page and 'hx-trigger="load"' in page
    part = client.get(f"/albums/{RG}/tracks", headers={"HX-Request": "true"}).text
    assert "Song" in part and 'hx-trigger="load"' not in part
    assert len(opener.requests) == 2
    covers = FakeOpener(404)
    monkeypatch.setattr(coverart, "client_for", lambda settings: caa_client(covers))
    client.get(f"/cover/{RG}")  # its page was opened, so its cover may be fetched
    assert len(covers.requests) == 1


def test_tracks_without_javascript_go_back_to_the_page(tmp_path: Path, monkeypatch):
    album_app(tmp_path, monkeypatch, RELEASE)
    with store_at(tmp_path) as store:
        cache_browse(store)
    client = TestClient(create_app(settings_for(tmp_path)), follow_redirects=False)
    back = client.get(f"/albums/{RG}/tracks", params={"edition": R[1]})
    assert (back.status_code, back.headers["location"]) == (
        303,
        f"/albums/{RG}?edition={R[1]}#tracks",
    )


def test_no_such_album(tmp_path: Path, monkeypatch):
    album_app(tmp_path, monkeypatch, 404)
    client = TestClient(create_app(settings_for(tmp_path)))
    assert client.get(f"/albums/{RG}").status_code == 404
    assert client.get("/albums/nope").status_code == 404


def test_without_a_contact_only_known_albums(tmp_path: Path):
    settings = Settings(database_path=tmp_path / "groovehound.db")
    with store_at(tmp_path) as store:
        own(store, R[1], RG, 1)
    client = TestClient(create_app(settings))
    page = client.get(f"/albums/{RG}").text
    assert "In your library" in page and "Not connected to MusicBrainz" in page
    assert 'hx-trigger="load"' not in page
    other = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    assert client.get(f"/albums/{other}").status_code == 404


def test_owned_tracks_are_marked(tmp_path: Path, monkeypatch):
    album_app(tmp_path, monkeypatch)
    with store_at(tmp_path) as store:
        own(store, R[1], RG, 1, tracks_rec := RELEASE["media"][0]["tracks"][0]["recording"]["id"])
        cache_browse(store)
        cache_tracks(store)
    page = TestClient(create_app(settings_for(tmp_path))).get(f"/albums/{RG}").text
    assert tracks_rec and "The edition you have" in page and "1 of 3 in your library" in page
    assert page.count('aria-label="In your library"') == 1
    assert page.count("Not in your library") == 2
    assert "Your copy" in page and "1 of 12 tracks" in page


def test_wishlist_item_on_its_album_page(tmp_path: Path, monkeypatch):
    album_app(tmp_path, monkeypatch)
    with store_at(tmp_path) as store:
        cache_browse(store)
        item = wish(store)
    client = TestClient(create_app(settings_for(tmp_path)), follow_redirects=False)
    page = client.get(f"/albums/{RG}").text
    assert "<h1>Wished</h1>" in page and "On your wishlist" in page
    assert f'action="/wishlist/{item.id}/notes"' in page and "Add to wishlist" not in page
    # its forms post without JavaScript and come back to the page
    back = client.post(f"/wishlist/{item.id}/notes", data={"notes": "n", "next": f"/albums/{RG}"})
    assert back.headers["location"] == f"/albums/{RG}"
    back = client.post(
        f"/wishlist/{item.id}/edition", data={"release": R[3], "next": f"/albums/{RG}"}
    )
    assert back.headers["location"].startswith(f"/albums/{RG}")
    page = client.get(f"/albums/{RG}").text
    assert "The edition on your wishlist" in page and "2020-06-01 · Digital Media" in page


def test_add_to_wishlist_from_the_album_page(tmp_path: Path, monkeypatch):
    album_app(tmp_path, monkeypatch)
    with store_at(tmp_path) as store:
        cache_browse(store)
    client = TestClient(create_app(settings_for(tmp_path)))
    page = client.get(f"/albums/{RG}").text
    assert 'class="primary">Add to wishlist' in page
    assert 'hx-post="/wishlist"' not in page  # a plain post: the page comes back with the item
    added = client.post(
        "/wishlist", data={"release_group": RG, "title": "Album", "next": f"/albums/{RG}"}
    )
    assert "Add notes" in added.text


def test_editions_link_to_their_track_lists(tmp_path: Path, monkeypatch):
    opener = album_app(tmp_path, monkeypatch, NAMED)
    with store_at(tmp_path) as store:
        own(store, R[1], RG, 1)
    client = TestClient(create_app(settings_for(tmp_path)))
    part = client.get(f"/albums/{RG}/editions", headers={"HX-Request": "true"}).text
    assert f'href="/albums/{RG}?edition={R[3]}#tracks"' in part
    assert "its tracks are shown" in part  # the library's, R[1]
    links = client.get(f"/albums/{RG}/links", headers={"HX-Request": "true"}).text
    assert "mb-links" in links
    assert len(opener.requests) == 1  # one browse for both


@pytest.mark.parametrize(
    "referer, tab",
    [
        ("", "Home"),
        ("http://x/search?title=a", "Search"),
        ("http://x/wishlist", "Wishlist"),
        ("http://x/artists/abc", "Library"),
        ("http://x/rediscover", "Home"),
    ],
)
def test_album_page_belongs_to_the_tab_it_came_from(tmp_path: Path, monkeypatch, referer, tab):
    album_app(tmp_path, monkeypatch)
    with store_at(tmp_path) as store:
        cache_browse(store)
    page = (
        TestClient(create_app(settings_for(tmp_path)))
        .get(f"/albums/{RG}", headers={"Referer": referer} if referer else {})
        .text
    )
    nav = page.split('<nav class="nav" aria-label="Main">')[1].split("</nav>")[0]
    assert 'aria-current="page">' in nav
    current = nav.split('aria-current="page">')[1].split("</a>")[0]
    assert f"<span>{tab}</span>" in current


def test_album_titles_link_to_album_pages(tmp_path: Path):
    with store_at(tmp_path) as store:
        wish(store)
    client = TestClient(create_app(settings_for(tmp_path)))
    assert f'<a class="tile-link" href="/albums/{RG}">' in client.get("/").text
    assert f'<a class="hit-title" href="/albums/{RG}">' in client.get("/wishlist").text


def test_an_edition_you_have_whole_is_listed_from_its_files(tmp_path: Path, monkeypatch):
    opener = album_app(tmp_path, monkeypatch)
    with store_at(tmp_path) as store:
        own_tagged(store, R[1], RG, *TWO_DISCS)
        listing = album.track_list(store, OWNER_ID, RG)
        assert listing.tracks.local and not listing.due
        assert listing.details == "2×CD · 3 tracks"  # noqa: RUF001
        cache_tracks(store, R[1])  # a cached lookup is shown instead
        assert not album.track_list(store, OWNER_ID, RG).tracks.local
    with store_at(tmp_path) as store:
        store.conn.execute("DELETE FROM api_cache")
        store.conn.commit()
    page = TestClient(create_app(settings_for(tmp_path))).get(f"/albums/{RG}").text
    assert "all in your library, listed from its files" in page and "Extra" in page
    assert 'hx-trigger="load"' not in page
    assert "Your copy" in page and " of 3 tracks" not in page
    assert opener.requests == []
