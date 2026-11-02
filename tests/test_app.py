from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from groovehound.brainz import musicbrainz
from groovehound.config import Settings, load_settings
from groovehound.db import OWNER_ID, Store
from groovehound.web import create_app

from .conftest import FakeOpener, settings_for
from .test_brainz import ARTIST, mb_client
from .test_search import R1, RECORDINGS, RG


def test_settings_precedence(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text(
        '[brainz]\ncontact = "file@example.com"\n[listenbrainz]\nuser = "me"\ntoken = "t"\n'
    )
    settings = load_settings(config, env={"GROOVEHOUND_CONTACT": "env@example.com"})
    assert settings.contact == "env@example.com"
    assert settings.listenbrainz_user == "me"
    assert "t" not in repr(settings).split("listenbrainz_user")[1]


def test_missing_explicit_config(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_settings(tmp_path / "nope.toml", env={})


def test_owner_exists_and_cache_round_trips(tmp_path: Path):
    with Store(tmp_path / "db.sqlite") as store:
        assert store.user(OWNER_ID).name == "owner"
        store.save_cached("musicbrainz", "release", "x", {"a": 1}, "2026-12-01T00:00:00+00:00")
        store.save_cached("musicbrainz", "release", "gone", None, "2026-11-02T00:00:00+00:00")
        assert store.cached("musicbrainz", "release", "x").data == {"a": 1}
        assert store.cached("musicbrainz", "release", "gone").data is None
        assert store.cached("musicbrainz", "release", "other") is None
        due = store.cache_due("musicbrainz", "release", "2026-11-15T00:00:00+00:00", 10)
        assert due == ["gone"]


def test_home_and_healthz(tmp_path: Path):
    client = TestClient(create_app(settings_for(tmp_path)))
    assert client.get("/healthz").text == "ok"
    page = client.get("/").text
    assert "MusicBrainz" in page and "ListenBrainz" in page  # attribution footer
    assert "Not connected" not in page


def test_home_explains_a_missing_contact(tmp_path: Path):
    client = TestClient(create_app(Settings(database_path=tmp_path / "db.sqlite")))
    assert "Not connected to MusicBrainz" in client.get("/").text


def test_bad_musicbrainz_settings_fail_at_startup(tmp_path: Path):
    with pytest.raises(ValueError):
        create_app(settings_for(tmp_path, musicbrainz_interval=0.5))


def search_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *answers) -> FakeOpener:
    """An app whose MusicBrainz client answers from a FakeOpener."""
    opener = FakeOpener(*answers)
    monkeypatch.setattr(musicbrainz, "client_for", lambda settings: mb_client(opener))
    return opener


def test_search_page_without_a_title_makes_no_request(tmp_path: Path, monkeypatch):
    opener = search_app(tmp_path, monkeypatch)
    client = TestClient(create_app(settings_for(tmp_path)))
    page = client.get("/search").text
    assert 'name="title"' in page
    assert opener.requests == []


def test_search_shows_results_and_saves_to_the_wishlist(tmp_path: Path, monkeypatch):
    opener = search_app(tmp_path, monkeypatch, RECORDINGS)
    client = TestClient(create_app(settings_for(tmp_path)))
    page = client.get("/search", params={"kind": "song", "title": "360", "artist": "charli"}).text
    assert "Brat" in page and "Hits 2024" in page
    assert "2024-06-07 · GB · 2×CD" in page  # noqa: RUF001  (an edition to choose)
    assert page.count("Add to wishlist") == 2

    added = client.post(
        "/wishlist",
        data={
            "release_group": RG.upper(),
            "release": R1,
            "title": "Brat",
            "artist_credit": "Charli xcx",
            "artist_mbids": ARTIST,
            "primary_type": "Album",
            "secondary_types": "",
            "first_release_date": "2024-06-01",
            "next": "/search?kind=song&title=360",
        },
        headers={"HX-Request": "true"},
    )
    assert "On your wishlist" in added.text

    page = client.get("/search", params={"kind": "song", "title": "360", "artist": "charli"}).text
    assert len(opener.requests) == 1  # the same search again comes from the cache
    assert page.count("Add to wishlist") == 1 and "On your wishlist" in page

    with Store(tmp_path / "groovehound.db") as store:
        (item,) = store.wishlist(OWNER_ID)
    assert (item.release_group_mbid, item.release_mbid) == (RG, R1)
    assert item.artist_mbids == (ARTIST,) and item.secondary_types == ()
    home = client.get("/").text
    assert "Brat" in home and "a specific edition" in home


def test_plain_form_post_goes_back_but_only_on_this_site(tmp_path: Path):
    client = TestClient(create_app(settings_for(tmp_path)), follow_redirects=False)
    form = {"release_group": RG, "title": "Brat", "artist_credit": "Charli xcx"}
    back = client.post("/wishlist", data={**form, "next": "/search?title=brat"})
    assert (back.status_code, back.headers["location"]) == (303, "/search?title=brat")
    for elsewhere in ("https://evil.example/", "//evil.example/", r"/\evil.example/"):
        moved = client.post("/wishlist", data={**form, "next": elsewhere})
        assert moved.headers["location"] == "/"
    with Store(tmp_path / "groovehound.db") as store:
        assert store.wishlist_count(OWNER_ID) == 1  # added once; the rest were already there


@pytest.mark.parametrize(
    "form",
    [
        {"release_group": "nope", "title": "Brat"},
        {"release_group": RG, "title": "  "},
        {"release_group": RG, "title": "Brat", "release": "nope"},
        {"release_group": RG, "title": "Brat", "artist_mbids": "nope"},
    ],
)
def test_wishlist_rejects_malformed_items(tmp_path: Path, form):
    client = TestClient(create_app(settings_for(tmp_path)))
    assert client.post("/wishlist", data=form).status_code == 400


def test_search_errors_are_shown_and_pause_searching(tmp_path: Path, monkeypatch):
    opener = search_app(tmp_path, monkeypatch, 500)
    client = TestClient(create_app(settings_for(tmp_path)))
    assert "MusicBrainz answered 500" in client.get("/search", params={"title": "x"}).text
    assert "paused" in client.get("/search", params={"title": "y"}).text
    assert len(opener.requests) == 1


def test_search_without_a_contact_explains_and_makes_no_request(tmp_path: Path, monkeypatch):
    opener = search_app(tmp_path, monkeypatch)
    client = TestClient(create_app(Settings(database_path=tmp_path / "db.sqlite")))
    assert "Not connected" in client.get("/search", params={"title": "x"}).text
    assert opener.requests == []


def wishlist_app(tmp_path: Path) -> tuple[TestClient, int]:
    """An app with one item (Brat) on the owner's wishlist, and that item's id."""
    client = TestClient(create_app(settings_for(tmp_path)), follow_redirects=False)
    client.post(
        "/wishlist", data={"release_group": RG, "title": "Brat", "artist_credit": "Charli xcx"}
    )
    with Store(tmp_path / "groovehound.db") as store:
        (item,) = store.wishlist(OWNER_ID)
    return client, item.id


def test_wishlist_page_notes_and_where_heard(tmp_path: Path):
    client, item_id = wishlist_app(tmp_path)
    page = client.get("/wishlist").text
    assert "Brat" in page and 'Still to buy <span class="muted">(1)' in page
    assert "Add notes" in page

    saved = client.post(
        f"/wishlist/{item_id}/notes",
        data={"heard": "  the café   on Main St ", "notes": "the 2×CD one\r\n\r\nask Sam  "},  # noqa: RUF001
        headers={"HX-Request": "true"},
    )
    assert saved.text.lstrip().startswith("<li") and "Heard: the café on Main St" in saved.text
    with Store(tmp_path / "groovehound.db") as store:
        item = store.wishlist_item(OWNER_ID, item_id)
    assert (item.heard, item.notes) == ("the café on Main St", "the 2×CD one\n\nask Sam")  # noqa: RUF001
    assert "Edit notes" in client.get("/wishlist").text


def test_bought_items_are_kept_but_hidden(tmp_path: Path):
    client, item_id = wishlist_app(tmp_path)
    gone = client.post(
        f"/wishlist/{item_id}/bought", data={"bought": "1"}, headers={"HX-Request": "1"}
    )
    assert gone.status_code == 200 and "<li" not in gone.text  # the item leaves the page
    assert 'hx-swap-oob="true"' in gone.text and 'Bought <span class="muted">(1)' in gone.text
    assert "Brat" not in client.get("/wishlist").text
    assert "Brat" not in client.get("/").text
    bought = client.get("/wishlist", params={"show": "bought"}).text
    assert "Brat" in bought and "Still want it" in bought

    with Store(tmp_path / "groovehound.db") as store:
        first = store.wishlist_item(OWNER_ID, item_id).bought_at
        assert first and store.mark_bought(OWNER_ID, item_id)  # again: keeps the first date
        assert store.wishlist_item(OWNER_ID, item_id).bought_at == first
        assert store.wishlist_count(OWNER_ID) == 0
        assert store.wishlist_count(OWNER_ID, bought=True) == 1

    back = client.post(
        f"/wishlist/{item_id}/bought", data={"bought": "0", "next": "/wishlist?show=bought"}
    )
    assert (back.status_code, back.headers["location"]) == (303, "/wishlist?show=bought")
    assert "Brat" in client.get("/wishlist").text


def test_remove_from_wishlist(tmp_path: Path):
    client, item_id = wishlist_app(tmp_path)
    removed = client.post(f"/wishlist/{item_id}/remove")
    assert (removed.status_code, removed.headers["location"]) == (303, "/wishlist")
    assert "Nothing on your wishlist yet" in client.get("/wishlist").text
    assert client.post(f"/wishlist/{item_id}/remove").status_code == 404


def test_wishlist_actions_only_touch_your_own_items(tmp_path: Path):
    client, item_id = wishlist_app(tmp_path)
    with Store(tmp_path / "groovehound.db") as store:
        store.conn.execute("INSERT INTO users (id, name, created_at) VALUES (2, 'other', '')")
        store.conn.commit()
        assert not store.mark_bought(2, item_id)
        assert not store.annotate_wishlist_item(2, item_id, "x", "y")
        assert not store.remove_from_wishlist(2, item_id)
        assert store.wishlist_item(2, item_id) is None
    for action in ("notes", "bought", "remove"):
        assert client.post(f"/wishlist/{item_id + 1}/{action}").status_code == 404


def test_home_links_to_the_whole_wishlist(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("groovehound.web.app.HOME_WISHLIST", 1)
    client, _ = wishlist_app(tmp_path)
    assert "All 1 on your wishlist" not in client.get("/").text
    client.post("/wishlist", data={"release_group": R1, "title": "Other", "artist_credit": "x"})
    assert "All 2 on your wishlist" in client.get("/").text
