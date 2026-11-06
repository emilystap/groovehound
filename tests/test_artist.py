from pathlib import Path

from fastapi.testclient import TestClient

from groovehound import artist, follows
from groovehound.db import OWNER_ID, Follow, Store
from groovehound.search import Hit
from groovehound.web import create_app

from .conftest import settings_for
from .test_app import search_app
from .test_follows import BROWSE, RG_EP, RG_OLD
from .test_library_artists import owning
from .test_search import ARTIST, GUEST, RG, RG_COMP

GROUPS = [f"0a1b2c3d-0000-4000-8000-0000000002{n:02d}" for n in range(5)]
LATER = "2099-01-01T00:00:00+00:00"


def cached(store: Store, mbid: str, tint: str = "") -> None:
    store.save_cover(mbid, b"jpeg", "image/jpeg", LATER, size=500, tint=tint)


def store_at(tmp_path: Path) -> Store:
    return Store(tmp_path / "groovehound.db")


def test_initials():
    assert artist.initials("Harbour Lights") == "HL"
    assert artist.initials("The Saltings of the Sea") == "TS"
    assert artist.initials("Björk") == "B"
    assert artist.initials("Tom & Jerry") == "TJ"
    assert artist.initials("!!!") == "?"


def test_a_mosaic_needs_four_cached_covers_else_the_newest_else_initials(tmp_path: Path):
    with store_at(tmp_path) as store:
        assert artist.picture(store, "Harbour Lights", GROUPS).covers == ()

        cached(store, GROUPS[1], "#123456")
        store.save_cover(GROUPS[0], None, "", LATER)  # known to have no cover: doesn't count
        found = artist.picture(store, "Harbour Lights", GROUPS)
        assert found.covers == (GROUPS[1],) and found.tint == "#123456"

        cached(store, GROUPS[2])
        cached(store, GROUPS[3], "#654321")
        assert artist.picture(store, "X", GROUPS).covers == (GROUPS[1],)  # three: still the one
        cached(store, GROUPS[4], "#abcdef")
        found = artist.picture(store, "X", GROUPS)
        assert found.covers == tuple(GROUPS[1:])
        assert found.tint == "#123456"  # the newest's, as it's first

        store.save_cover(GROUPS[1], b"jpeg", "image/jpeg", LATER)  # newest has no usable tint
        assert artist.picture(store, "X", GROUPS).tint == "#654321"


def test_the_picture_prefers_albums():
    hit = Hit("a", "A", "X", primary_type="Album")
    live = Hit("b", "B", "X", primary_type="Album", secondary_types=("Live",))
    found = follows.Discography(follows.Artist("x", "X"), (live, hit), 2, "2026-11-01")
    assert artist.release_groups(found) == ["a", "b"]
    assert artist.release_groups(None) == []


def test_summary_says_what_they_are_and_how_much_there_is():
    hits = (
        Hit("a", "A", "X", primary_type="Album"),
        Hit("b", "B", "X", primary_type="EP"),
        Hit("c", "C", "X", primary_type="EP"),
        Hit("d", "D", "X", primary_type="Album", secondary_types=("Live",)),
    )
    found = follows.Discography(
        follows.Artist("x", "X", type="Group", disambiguation="Bristol folk"), hits, 4, ""
    )
    assert artist.summary(found) == "Group · Bristol folk · 1 album, 2 EPs"
    bare = follows.Discography(follows.Artist("x", "X"), (), 0, "")
    assert artist.summary(bare) == "" and artist.summary(None) == ""


def test_tiles_say_what_each_is_and_offer_only_the_other_artists():
    duet = Hit(
        "a",
        "Duet",
        "X & Y",
        ("x", "y"),
        ("X", "Y"),
        primary_type="Album",
        first_release_date="2019-03-01",
    )
    soon = Hit("b", "Soon", "X", ("x",), ("X",), first_release_date="2026-12-24")
    first, second = artist.tiles((duet, soon), "x", "2026-11-04", served={"a"})
    assert first.artist == "Album · 2019" and first.detail == "X & Y"
    assert [c.name for c in first.credits] == ["Y"] and first.hit is duet
    assert not first.archive and first.badge == ""
    assert second.badge == "Out 24 Dec" and second.archive and second.detail == ""
    assert not any(t.archive for t in artist.tiles((duet, soon), "x", "2026-11-04"))


def test_the_page_has_the_grid_folds_and_one_picture(tmp_path: Path, monkeypatch):
    search_app(tmp_path, monkeypatch, BROWSE)
    client = TestClient(create_app(settings_for(tmp_path)))
    with store_at(tmp_path) as store:
        owning(store)
        for mbid, tint in ((RG, "#336699"), (RG_OLD, ""), (RG_EP, ""), (RG_COMP, "")):
            cached(store, mbid, tint)
    page = client.get(f"/artists/{ARTIST}").text
    assert "<h1>Charli xcx</h1>" in page and "--band-dark:" in page
    assert page.count('class="artist-picture') == 1 and 'class="artist-picture one' not in page
    assert 'class="grid"' in page and 'class="fold"' in page
    assert "Live, compilations, remixes and more" in page
    assert 'aria-current="page">All</a>' in page
    assert ">Open on MusicBrainz<" in page and f'/artist/{ARTIST}" rel="noopener"' in page
    # a cover already cached is served, even for an artist you don't follow
    assert client.get(f"/cover/{RG_OLD}").status_code == 200
    page = client.get(f"/artists/{ARTIST}?missing=1").text
    assert 'aria-current="page">Missing only · 2</a>' in page
    assert page.count('class="artist-picture') == 1  # all their covers, not only what's missing


def test_a_cover_nothing_knows_is_still_refused(tmp_path: Path, monkeypatch):
    search_app(tmp_path, monkeypatch, BROWSE)
    client = TestClient(create_app(settings_for(tmp_path)))
    assert client.get(f"/cover/{GROUPS[0]}").status_code == 404


def test_someone_unseen_gets_a_monogram(tmp_path: Path, monkeypatch):
    search_app(tmp_path, monkeypatch, BROWSE)
    client = TestClient(create_app(settings_for(tmp_path)))
    page = client.get(f"/artists/{ARTIST}").text
    assert '<span class="artist-picture monogram" aria-hidden="true">CX</span>' in page
    assert "--band-dark:" not in page


def test_a_library_artist_is_named_by_the_files_when_nothing_else_can(tmp_path: Path, monkeypatch):
    from groovehound.config import Settings

    client = TestClient(create_app(Settings(database_path=tmp_path / "groovehound.db")))
    with store_at(tmp_path) as store:
        owning(store)
    page = client.get(f"/artists/{ARTIST}").text
    assert "<h1>Charli xcx</h1>" in page and ">CX</span>" in page


def test_release_types_are_a_sheet_that_works_without_javascript(tmp_path: Path, monkeypatch):
    search_app(tmp_path, monkeypatch, BROWSE)
    client = TestClient(create_app(settings_for(tmp_path)), follow_redirects=False)
    with store_at(tmp_path) as store:
        store.follow(OWNER_ID, Follow(ARTIST, "Charli xcx"))
    here = f"/artists/{ARTIST}"
    page = " ".join(client.get(here).text.split())
    assert "New releases: Albums, EPs ·" in page and f'popovertarget="types-{ARTIST}"' in page
    assert f'id="types-{ARTIST}" popover' in page
    assert "New releases: Albums, EPs ·" in client.get("/artists").text

    back = client.post(f"{here}/types", data={"type": ["album"], "next": here})
    assert back.status_code == 303 and back.headers["location"] == here
    assert "New releases: Albums ·" in " ".join(client.get(here).text.split())


def test_similar_artists_are_a_shelf_with_dismiss_in_the_menu(tmp_path: Path, monkeypatch):
    from .test_similar import ARCADE_FIRE, RADIO, radio_app

    radio_app(tmp_path, monkeypatch, RADIO)
    client = TestClient(create_app(settings_for(tmp_path)))
    card = client.post(f"/artists/{ARTIST}/similar", headers={"HX-Request": "true"}).text
    assert 'class="shelf similar"' in card and 'class="tile artist-tile"' in card
    assert f'id="menu-similar-{ARCADE_FIRE}" popover' in card
    assert f'action="/artists/{ARCADE_FIRE}/dismiss"' in card
    assert '<span class="artist-picture monogram" aria-hidden="true">AF</span>' in card
    assert GUEST not in card
