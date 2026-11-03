import random
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from groovehound import buy, releases
from groovehound.brainz import musicbrainz
from groovehound.brainz.http import ServiceError, ServicePause
from groovehound.db import OWNER_ID, Store, WishlistItem
from groovehound.web import create_app

from .conftest import FakeClock, FakeOpener, settings_for
from .test_brainz import mb_client, params
from .test_search import R1, R2, RG

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
ITEM = WishlistItem(release_group_mbid=RG, title="Brat", artist_credit="Charli xcx", id=1)


def url_rel(kind: str, url: str, **extra) -> dict:
    return {"type": kind, "target-type": "url", "url": {"resource": url}, **extra}


# A browse of the release group's releases with inc=media labels url-rels (the release groups
# and credits it also asks for are left out), as MusicBrainz shapes it
RELEASES = {
    "release-count": 2,
    "releases": [
        {
            "id": R1,
            "title": "Brat",
            "status": "Official",
            "date": "2024-06-07",
            "country": "GB",
            "barcode": "075678612345",
            "media": [{"format": "CD", "position": 1, "track-count": 15}],
            "label-info": [{"catalog-number": "075678", "label": {"name": "Atlantic"}}],
            "relations": [
                url_rel("streaming", "https://open.spotify.com/album/r1"),
                url_rel("purchase for download", "https://www.qobuz.com/gb-en/album/brat/r1"),
                url_rel("discogs", "https://www.discogs.com/release/1"),  # not a store
            ],
        },
        {
            "id": R2,
            "title": "Brat",
            "status": "Official",
            "date": "2024-06-07",
            "country": "XW",
            "media": [{"format": "Digital Media", "position": 1, "track-count": 15}],
            "label-info": [],
            "relations": [
                url_rel("purchase for download", "https://charlixcx.bandcamp.com/album/brat"),
                url_rel("purchase for mail-order", "https://charlixcx.bandcamp.com/album/brat"),
                url_rel("streaming", "https://open.spotify.com/album/r2"),  # Spotify again
                url_rel("streaming", "https://music.youtube.com/playlist?list=x"),
                url_rel("purchase for download", "https://itunes.apple.com/old", ended=True),
                url_rel("purchase for download", "javascript:alert(1)"),
            ],
        },
    ],
}


def test_search_links_are_built_locally():
    bandcamp, qobuz = buy.search_links(ITEM, "gb-en")
    assert bandcamp.url == "https://bandcamp.com/search?q=Charli+xcx+Brat&item_type=a"
    assert qobuz.url == "https://www.qobuz.com/gb-en/search/albums/Charli%20xcx%20Brat"
    with pytest.raises(ValueError, match="qobuz"):
        buy.search_links(ITEM, "../evil")


def test_musicbrainz_links_one_per_store_and_kind():
    links = buy.musicbrainz_links(RELEASES)
    assert [(link.kind, link.site, link.note) for link in links] == [
        ("stream", "Spotify", ""),
        ("buy", "Qobuz", ""),
        ("buy", "Bandcamp", ""),
        ("buy", "Bandcamp", "physical"),
        ("stream", "YouTube Music", ""),
    ]
    assert links[0].url.endswith("/r1")


def test_the_chosen_edition_comes_first():
    links = buy.musicbrainz_links(RELEASES, release_mbid=R2)
    assert links[0].site == "Bandcamp"
    assert next(link for link in links if link.site == "Spotify").url.endswith("/r2")


def test_site_names():
    assert buy.site_name("https://someone.bandcamp.com/album/x") == "Bandcamp"
    assert buy.site_name("https://music.youtube.com/x") == "YouTube Music"
    assert buy.site_name("https://www.youtube.com/x") == "YouTube"
    assert buy.site_name("https://shop.example.org/x") == "shop.example.org"


def test_links_are_one_request_then_cached(tmp_path: Path):
    opener = FakeOpener(RELEASES)
    client, pause = mb_client(opener), ServicePause()
    with Store(tmp_path / "db.sqlite") as store:
        before = buy.where_to_buy(store, ITEM, "us-en", now=NOW)
        assert before.links is None and before.due and len(before.searches) == 2

        links = buy.fetch_links(store, client, pause, ITEM, now=NOW, rng=random.Random(1))
        assert buy.fetch_links(store, client, pause, ITEM, now=NOW) == links
        (request,) = opener.requests
        assert urllib.parse.urlsplit(request.full_url).path == "/ws/2/release"
        assert params(request)["release-group"] == [RG]
        assert params(request)["inc"] == ["media labels url-rels release-groups artist-credits"]

        after = buy.where_to_buy(store, ITEM, "us-en", now=NOW)
        assert after.links == links and not after.due
        assert [link.site for link in after.buy] == ["Qobuz", "Bandcamp", "Bandcamp"]

        refresh = store.cached("musicbrainz", releases.CACHE_KIND, RG).refresh_after
        low, high = releases.TTL
        assert (NOW + low).isoformat() <= refresh <= (NOW + high).isoformat()
        later = NOW + high
        assert buy.where_to_buy(store, ITEM, "us-en", now=later).due
        again = FakeOpener({"releases": []})
        assert buy.fetch_links(store, mb_client(again), pause, ITEM, now=later) == ()
        assert len(again.requests) == 1


def test_a_vanished_release_group_is_remembered(tmp_path: Path):
    with Store(tmp_path / "db.sqlite") as store:
        opener = FakeOpener(404)
        assert buy.fetch_links(store, mb_client(opener), ServicePause(), ITEM, now=NOW) == ()
        assert buy.where_to_buy(store, ITEM, "us-en", now=NOW).links == ()


def test_failure_pauses_lookups(tmp_path: Path):
    pause = ServicePause(clock=FakeClock())
    with Store(tmp_path / "db.sqlite") as store:
        with pytest.raises(ServiceError):
            buy.fetch_links(store, mb_client(FakeOpener(500)), pause, ITEM, now=NOW)
        refused = FakeOpener()
        with pytest.raises(ServiceError, match="paused"):
            buy.fetch_links(store, mb_client(refused), pause, ITEM, now=NOW)
        assert refused.requests == []
        assert buy.where_to_buy(store, ITEM, "us-en", now=NOW).links is None


def links_app(tmp_path: Path, monkeypatch, *answers, **settings) -> tuple[TestClient, FakeOpener]:
    opener = FakeOpener(*answers)
    monkeypatch.setattr(musicbrainz, "client_for", lambda settings: mb_client(opener))
    client = TestClient(create_app(settings_for(tmp_path, **settings)), follow_redirects=False)
    client.post(
        "/wishlist", data={"release_group": RG, "title": "Brat", "artist_credit": "Charli xcx"}
    )
    return client, opener


def item_id(tmp_path: Path) -> int:
    with Store(tmp_path / "groovehound.db") as store:
        (item,) = store.wishlist(OWNER_ID)
    return item.id


def test_wishlist_page_shows_searches_and_asks_musicbrainz_only_when_opened(
    tmp_path: Path, monkeypatch
):
    client, opener = links_app(tmp_path, monkeypatch, RELEASES, qobuz_store="fr-fr")
    page = client.get("/wishlist").text
    assert "Where to buy" in page and "qobuz.com/fr-fr/search/albums/Charli%20xcx%20Brat" in page
    item = item_id(tmp_path)
    assert f'hx-get="/wishlist/{item}/links"' in page
    assert opener.requests == []  # nothing asked when the item was added or the page shown

    fragment = client.get(f"/wishlist/{item}/links", headers={"HX-Request": "1"}).text
    assert "charlixcx.bandcamp.com/album/brat" in fragment and "(physical)" in fragment
    assert "javascript:" not in fragment
    page = client.get("/wishlist").text
    assert "open.spotify.com/album/r1" in page and "hx-get" not in page  # cached, not due
    assert len(opener.requests) == 1


def test_links_without_javascript_go_back_to_the_item(tmp_path: Path, monkeypatch):
    client, _ = links_app(tmp_path, monkeypatch, {"releases": []})
    item = item_id(tmp_path)
    back = client.get(f"/wishlist/{item}/links", params={"next": "//evil.example"})
    assert (back.status_code, back.headers["location"]) == (303, f"/#wish-{item}")
    assert "no purchase or streaming links" in client.get("/wishlist").text
    assert client.get(f"/wishlist/{item + 1}/links").status_code == 404


def test_links_errors_are_shown(tmp_path: Path, monkeypatch):
    client, _ = links_app(tmp_path, monkeypatch, 500)
    fragment = client.get(f"/wishlist/{item_id(tmp_path)}/links", headers={"HX-Request": "1"})
    assert "MusicBrainz answered 500" in fragment.text


def test_bad_qobuz_store_fails_at_startup(tmp_path: Path):
    with pytest.raises(ValueError, match="qobuz"):
        create_app(settings_for(tmp_path, qobuz_store="GB"))
