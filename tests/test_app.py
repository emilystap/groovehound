import struct
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from groovehound.brainz import musicbrainz
from groovehound.config import Settings, load_settings
from groovehound.db import OWNER_ID, Store, local_date
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
    assert "listenbrainz_token" not in repr(settings)


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


def test_installable_as_an_app(tmp_path: Path):
    """What Chrome needs before it offers *Install app*: a linked manifest, served as one, with
    a name, start URL in scope, standalone display and 192 / 512 px PNG icons (maskable too)."""
    client = TestClient(create_app(settings_for(tmp_path)))
    assert '<link rel="manifest" href="/static/manifest.webmanifest">' in client.get("/").text
    response = client.get("/static/manifest.webmanifest")
    assert response.headers["content-type"].startswith("application/manifest+json")
    manifest = response.json()
    assert manifest["name"] and manifest["display"] == "standalone"
    assert manifest["start_url"].startswith(manifest["scope"]) and manifest["scope"] == "/"
    found = set()
    for icon in manifest["icons"]:
        assert icon["src"].startswith("/")  # root-relative, for the reverse proxy
        png = client.get(icon["src"])
        assert png.headers["content-type"] == "image/png"
        assert png.content.startswith(b"\x89PNG\r\n\x1a\n")
        width, height = struct.unpack(">II", png.content[16:24])  # IHDR
        assert icon["sizes"] == f"{width}x{height}"
        found.add((icon["purpose"], width))
    assert {("any", 192), ("any", 512), ("maskable", 512)} <= found


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


def test_quick_capture_and_remove(tmp_path: Path):
    client = TestClient(create_app(settings_for(tmp_path)), follow_redirects=False)
    assert "Quick capture" in client.get("/").text and "To look up" not in client.get("/").text

    saved = client.post("/captures", data={"text": "  sax song   at the café ", "next": "/"})
    assert (saved.status_code, saved.headers["location"]) == (303, "/")
    card = client.post(
        "/captures", data={"text": "x" * 600, "next": "/wishlist"}, headers={"HX-Request": "1"}
    )
    assert card.text.lstrip().startswith("<section") and "To look up (2)" in card.text
    assert 'value="/wishlist"' in card.text
    assert client.post("/captures", data={"text": "   "}).status_code == 400

    with Store(tmp_path / "groovehound.db") as store:
        long, sax = store.captures(OWNER_ID)
    assert sax.text == "sax song at the café" and len(long.text) == 500
    assert "sax song at the café" in client.get("/wishlist").text
    assert "sax song" not in client.get("/wishlist", params={"show": "bought"}).text

    removed = client.post(f"/captures/{sax.id}/remove", data={"next": "/wishlist"})
    assert (removed.status_code, removed.headers["location"]) == (303, "/wishlist")
    assert client.post(f"/captures/{sax.id}/remove").status_code == 404
    with Store(tmp_path / "groovehound.db") as store:
        assert store.captures(OWNER_ID) == [long]
        store.conn.execute("INSERT INTO users (id, name, created_at) VALUES (2, 'other', '')")
        store.conn.commit()
        assert store.captures(2) == [] and store.capture(2, long.id) is None
        assert not store.remove_capture(2, long.id)


def test_a_shared_song_fills_in_the_search_without_searching(tmp_path: Path, monkeypatch):
    opener = search_app(tmp_path, monkeypatch)
    client = TestClient(create_app(settings_for(tmp_path)))
    manifest = client.get("/static/manifest.webmanifest").json()
    assert manifest["share_target"]["action"] == "/share"
    page = client.get(
        "/share",
        params={
            "text": "Stand by Me by Ben E. King\n"
            "https://www.google.com/search?q=Stand+by+Me+by+Ben+E.+King"
        },
    ).text
    assert 'value="song" checked' in page
    assert 'name="title" value="Stand by Me"' in page
    assert 'name="artist" value="Ben E. King"' in page
    assert "Shared to Groovehound" in page
    assert opener.requests == []  # you still press Search


def test_a_share_that_does_not_parse_fills_in_quick_capture(tmp_path: Path):
    client = TestClient(create_app(settings_for(tmp_path)))
    page = client.get("/share", params={"title": "Look", "text": "sax song  at the café"}).text
    assert 'value="Look sax song at the café"' in page and "Quick capture" in page
    with Store(tmp_path / "groovehound.db") as store:
        assert store.captures(OWNER_ID) == []  # saved only when you press Save


def test_looking_up_a_capture_files_it_on_the_item(tmp_path: Path, monkeypatch):
    opener = search_app(tmp_path, monkeypatch, RECORDINGS)
    client = TestClient(create_app(settings_for(tmp_path)))
    with Store(tmp_path / "groovehound.db") as store:
        capture_id = store.add_capture(OWNER_ID, "sax song at the café")
        other_id = store.add_capture(OWNER_ID, "something else")

    page = client.get("/search", params={"capture": capture_id}).text
    assert "Looking up what you wrote down" in page and "sax song at the café" in page
    assert f'name="capture" value="{capture_id}"' in page and opener.requests == []

    params = {"kind": "song", "title": "360", "capture": capture_id}
    page = client.get("/search", params=params).text
    assert page.count(f'name="capture" value="{capture_id}"') == 3  # the search and two results

    form = {"release_group": RG, "title": "Brat", "artist_credit": "Charli xcx"}
    added = client.post(
        "/wishlist", data={**form, "capture": capture_id}, headers={"HX-Request": "1"}
    )
    assert "On your wishlist" in added.text and 'hx-swap-oob="true"' in added.text
    with Store(tmp_path / "groovehound.db") as store:
        (item,) = store.wishlist(OWNER_ID)
        assert item.notes.startswith("sax song at the café (20")
        assert [c.id for c in store.captures(OWNER_ID)] == [other_id]
        # a second capture filed on the same item goes on a new line of its notes
        assert store.file_capture(OWNER_ID, other_id, RG)
        assert store.wishlist_item(OWNER_ID, item.id).notes.splitlines()[1].startswith("something")
        assert not store.file_capture(OWNER_ID, other_id, RG)  # already filed

    # once filed, the capture is gone: its id is ignored, and the page just searches
    again = client.post("/wishlist", data={**form, "capture": capture_id})
    assert again.status_code == 200
    assert "Looking up" not in client.get("/search", params={"capture": capture_id}).text


def test_a_search_can_be_saved_as_a_quick_capture(tmp_path: Path, monkeypatch):
    opener = search_app(tmp_path, monkeypatch, RECORDINGS)
    client = TestClient(create_app(settings_for(tmp_path)), follow_redirects=False)
    assert "Not what you were looking for" not in client.get("/search").text  # nothing searched

    params = {"kind": "song", "title": "360", "artist": "Charli xcx"}
    page = client.get("/search", params=params).text
    assert "Not what you were looking for" in page
    assert 'value="360 by Charli xcx (song)"' in page

    form = {"text": "360 by Charli xcx (song)", "from": "search", "next": "/search?title=360"}
    saved = client.post("/captures", data=form, headers={"HX-Request": "1"})
    assert 'id="capture-search"' in saved.text and "Saved “" in saved.text
    back = client.post("/captures", data={**form, "text": "360 (album)"})  # without JavaScript
    assert (back.status_code, back.headers["location"]) == (303, "/search?title=360")
    with Store(tmp_path / "groovehound.db") as store:
        album, song = store.captures(OWNER_ID)
    assert (song.text, album.text) == ("360 by Charli xcx (song)", "360 (album)")
    assert len(opener.requests) == 1  # saving asks MusicBrainz nothing

    # looking up a capture already keeps the text, so there's nothing to save
    params["capture"] = song.id
    assert "Not what you were looking for" not in client.get("/search", params=params).text


def test_a_capture_is_kept_if_the_item_is_not_saved(tmp_path: Path):
    with Store(tmp_path / "db.sqlite") as store:
        capture_id = store.add_capture(OWNER_ID, "sax song")
        assert not store.file_capture(OWNER_ID, capture_id, RG)  # not on the wishlist
        assert store.capture(OWNER_ID, capture_id).text == "sax song"


def test_dates_are_shown_in_the_tz_time_zone(tmp_path: Path, monkeypatch):
    late = "2026-11-02T03:45:29+00:00"  # still the evening of 1 November in New York
    monkeypatch.setenv("TZ", "America/New_York")
    assert local_date(late) == "2026-11-01" and local_date(None) == ""
    monkeypatch.setenv("TZ", "Etc/UTC")
    assert local_date(late) == "2026-11-02"
    monkeypatch.setenv("TZ", "EST5EDT,M3.2.0,M11.1.0")  # not an IANA name: the system's zone
    assert local_date(late) in ("2026-11-01", "2026-11-02")

    monkeypatch.setenv("TZ", "America/New_York")
    client, item_id = wishlist_app(tmp_path)
    with Store(tmp_path / "groovehound.db") as store:
        store.conn.execute("UPDATE wishlist SET added_at = ?", (late,))
        store.conn.execute(
            "INSERT INTO captures (user_id, text, captured_at) VALUES (?, 'sax song', ?)",
            (OWNER_ID, late),
        )
        store.conn.commit()
        (capture,) = store.captures(OWNER_ID)
    page = client.get("/wishlist").text
    assert "Added 2026-11-01" in page and "sax song</span>\n      <span" in page
    assert "2026-11-01</span>" in page and "2026-11-02" not in page
    with Store(tmp_path / "groovehound.db") as store:
        assert store.file_capture(OWNER_ID, capture.id, RG)
        assert store.wishlist_item(OWNER_ID, item_id).notes == "sax song (2026-11-01)"
