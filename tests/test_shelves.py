from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from groovehound import rediscover, shelves
from groovehound.db import OWNER_ID, Store, WishlistItem
from groovehound.rediscover import Pick
from groovehound.search import Hit
from groovehound.web import create_app

from .conftest import settings_for
from .test_rediscover import NOW, USER, rg, set_up

TODAY = "2026-11-04"
RG_A = "0a1b2c3d-0000-4000-8000-0000000000a1"


@pytest.mark.parametrize(
    "date, label",
    [
        ("2026-11-14", "Out 14 Nov"),
        ("2027-01-05", "Out 5 Jan 2027"),
        ("2026-11", "Out Nov 2026"),
        ("2027", "Out 2027"),
        ("soon", "Out soon"),
        ("2026-13-01", "Out 2026-13-01"),
    ],
)
def test_a_release_date_on_a_badge(date, label):
    assert shelves.out_label(date, TODAY) == label


@pytest.mark.parametrize(
    "pick, badge",
    [
        (Pick(RG_A, "A", "B", "", hit=Hit(RG_A, "A", "B")), "New"),
        (Pick(RG_A, "A", "B", ""), "Never played"),
        (Pick(RG_A, "A", "B", "", 3, "2026-03-10T12:00:00+00:00"), "Not since March"),
        (Pick(RG_A, "A", "B", "", 3, "2025-09-10T12:00:00+00:00"), "Not since Sep 2025"),
        (Pick(RG_A, "A", "B", "", 3), "Not played lately"),
    ],
)
def test_a_tile_says_why_it_was_drawn(pick, badge):
    (tile,) = shelves.picks([pick], TODAY)
    assert tile.badge == badge and tile.dismiss == f"/rediscover/{RG_A}/dismiss"


def test_empty_shelves_are_left_out(tmp_path: Path):
    with Store(tmp_path / "db.sqlite") as store:
        assert shelves.home(store, OWNER_ID, "") == []


def test_shelves_come_in_order(tmp_path: Path):
    with Store(tmp_path / "db.sqlite") as store:
        set_up(store)
        store.add_to_wishlist(OWNER_ID, WishlistItem(RG_A, "Wanted", "Someone"))
        (item,) = store.wishlist(OWNER_ID)
        store.annotate_wishlist_item(OWNER_ID, item.id, "", "the café")
        found = shelves.home(store, OWNER_ID, USER, now=NOW)
    assert [s.key for s in found] == ["next", "wishlist"]
    following, wished = found
    assert following.shuffle and following.see_all.startswith("/rediscover?draw=")
    assert wished.rows and wished.tiles[0].detail == "Heard: the café"
    assert wished.tiles[0].hit is None and not wished.tiles[0].dismiss


def test_home_and_shuffle(tmp_path: Path):
    settings = settings_for(tmp_path, listenbrainz_user=USER)
    with Store(settings.database_path) as store:
        set_up(store)
    client = TestClient(create_app(settings), follow_redirects=False)
    page = client.get("/").text
    assert 'id="next"' in page and 'action="/shelves/next"' in page
    assert f'id="next-{rg(2)}"' in page and 'popovertarget="menu-next-' in page
    assert '<span class="badge">Never played</span>' in page

    # without JavaScript: back to the shelf; with HTMX: the shelf alone
    plain = client.get("/shelves/next")
    assert (plain.status_code, plain.headers["location"]) == (303, "/#next")
    shelf = client.get("/shelves/next", headers={"HX-Request": "1"}).text
    assert shelf.lstrip().startswith('<section class="shelf" id="next">')
    assert 'value="/"' in shelf  # its forms come back to the home page


def test_see_all_has_what_the_shelf_showed_and_more(tmp_path: Path, monkeypatch):
    settings = settings_for(tmp_path, listenbrainz_user=USER)
    with Store(settings.database_path) as store:
        set_up(store)
        for section in ("FAVOURITES", "OTHERS", "NOT_LATELY", "NEW"):
            monkeypatch.setattr(rediscover, section, 1)
        (shelf,) = shelves.home(store, OWNER_ID, USER, now=NOW)
    client = TestClient(create_app(settings))
    page = client.get(shelf.see_all).text
    assert all(f'href="/albums/{t.mbid}"' in page for t in shelf.tiles)
    assert page.count('class="tile"') > len(shelf.tiles)
    assert f'value="{shelf.see_all}"' in page  # a dismissal comes back to the same draw


def test_shuffle_with_nothing_left_to_draw(tmp_path: Path):
    client = TestClient(create_app(settings_for(tmp_path)))
    assert client.get("/shelves/next", headers={"HX-Request": "1"}).text == ""


def test_dismissing_from_a_tile_comes_back_without_javascript(tmp_path: Path):
    settings = settings_for(tmp_path, listenbrainz_user=USER)
    with Store(settings.database_path) as store:
        set_up(store)
    client = TestClient(create_app(settings), follow_redirects=False)
    page = client.get("/rediscover").text
    assert f'action="/rediscover/{rg(2)}/dismiss"' in page
    back = client.post(f"/rediscover/{rg(2)}/dismiss", data={"next": "/"})
    assert (back.status_code, back.headers["location"]) == (303, "/")
    assert f'id="next-{rg(2)}"' not in client.get("/").text


def test_quick_capture_from_home(tmp_path: Path):
    client = TestClient(create_app(settings_for(tmp_path)))
    assert '<a class="button" href="/wishlist#captures">Quick capture</a>' in client.get("/").text
    client.post("/captures", data={"text": "sax song"})
    page = client.get("/").text
    assert "To look up · 1" in page and "sax song" not in page  # the list is on the wishlist
    shared = client.get("/share", params={"text": "something odd"}).text
    assert 'value="something odd"' in shared  # shared text still waits in the form


def test_the_new_page(tmp_path: Path):
    page = TestClient(create_app(settings_for(tmp_path))).get("/new").text
    assert "Follow artists to see their new releases here." in page
    assert '<a href="/" aria-current="page">' in page  # it belongs to Home
