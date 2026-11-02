from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from groovehound import buy, releases
from groovehound.brainz.http import ServicePause
from groovehound.db import Store
from groovehound.web import create_app

from .conftest import FakeOpener, settings_for
from .test_app import search_app
from .test_brainz import mb_client
from .test_buy import ITEM, RELEASES, item_id, links_app
from .test_search import R1, R2, RELEASE_GROUPS, RG

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
R = [f"{n:08x}-2222-2222-2222-{'2' * 12}" for n in range(6)]
DIGITAL = [{"format": "Digital Media", "track-count": 12}]


def edition(mbid: str, **extra) -> dict:
    return {"id": mbid, "title": "BITE ME", "media": [], "label-info": [], **extra}


# Like the editions of Reneé Rapp's BITE ME: several digital releases alike in all shown
BITE_ME = {
    "releases": [
        edition(R[0], status="Promotion", date="2025-07-01", media=DIGITAL),
        edition(R[1], status="Official", date="2025-08-01", country="XW", media=DIGITAL),
        edition(R[2], status="Official", media=[{"format": "CD"}, {"format": "CD"}]),
        edition(R[3], status="Official", date="2025-08-01", country="XW", media=DIGITAL),
        edition(
            R[4],
            status="Official",
            date="2025-08-01",
            country="US",
            title="BITE ME (deluxe)",
            disambiguation="explicit",
            barcode="0123",
            media=[{"format": '12" Vinyl', "track-count": 7}, {"format": "CD", "track-count": 7}],
            **{
                "label-info": [
                    {"catalog-number": "B1", "label": {"name": "Interscope"}},
                    {"catalog-number": "B2", "label": {"name": "Interscope"}},
                ]
            },
        ),
        edition(R[5], status="Official", date="2025-08-01", country="XW", media=DIGITAL),
    ]
}


def test_editions_tell_apart_what_musicbrainz_shows():
    rows = releases.editions(BITE_ME, "BITE ME")
    assert [(e.details, e.labels) for e in rows] == [
        ("2025-08-01 · Worldwide · Digital Media · 12 tracks", ""),
        (
            '2025-08-01 · US · 12" Vinyl + CD · 14 tracks',
            "BITE ME (deluxe) · Interscope · B1, B2 · explicit",  # no barcode,
        ),
        ("2×CD", ""),  # noqa: RUF001  (undated: after the dated ones)
        ("2025-07-01 · Digital Media · 12 tracks · Promotion", ""),  # not official: last
    ]
    assert rows[0].mbids == (R[1], R[3], R[5])  # identical: one row, the first one saved
    assert rows[0].mbid == R[1]
    assert [e.official for e in rows] == [True, True, True, False]


def test_choice_before_and_after_asking(tmp_path: Path):
    item = replace(ITEM, release_mbid=R2)
    with Store(tmp_path / "db.sqlite") as store:
        before = releases.choice(store, item, now=NOW)
        assert before.editions is None and before.due and before.chosen is None
        releases.fetch(store, mb_client(FakeOpener(RELEASES)), ServicePause(), RG, now=NOW)
        after = releases.choice(store, item, now=NOW)
        assert not after.due and [e.mbids for e in after.editions] == [(R1,), (R2,)]
        assert after.chosen.details == "2024-06-07 · Worldwide · Digital Media · 15 tracks"


def test_a_response_cached_before_editions_is_asked_for_again(tmp_path: Path):
    old = {"releases": [{"id": R1, "relations": RELEASES["releases"][0]["relations"]}]}
    with Store(tmp_path / "db.sqlite") as store:
        store.save_cached("musicbrainz", releases.CACHE_KIND, RG, old, "2027-01-01T00:00:00+00:00")
        where = buy.where_to_buy(store, ITEM, "us-en", now=NOW)
        assert where.links and where.due  # its links still show until it's opened
        assert releases.choice(store, ITEM, now=NOW).editions is None

        opener = FakeOpener(RELEASES)
        buy.fetch_links(store, mb_client(opener), ServicePause(), ITEM, now=NOW)
        assert len(opener.requests) == 1
        assert not buy.where_to_buy(store, ITEM, "us-en", now=NOW).due


def test_choose_edition_on_the_wishlist(tmp_path: Path, monkeypatch):
    client, opener = links_app(tmp_path, monkeypatch, RELEASES)
    item = item_id(tmp_path)
    page = client.get("/wishlist").text
    assert "Choose edition" in page and f'hx-get="/wishlist/{item}/editions?next=' in page
    assert opener.requests == []

    hx = {"HX-Request": "1"}
    fragment = client.get(f"/wishlist/{item}/editions", headers=hx).text
    assert "2024-06-07 · GB · CD · 15 tracks" in fragment
    assert "Atlantic · 075678" in fragment and "075678612345" not in fragment  # no barcode
    assert f'value="{R2}"' in fragment
    client.get(f"/wishlist/{item}/links", headers=hx)  # the same browse: no second request
    assert len(opener.requests) == 1

    chosen = client.post(
        f"/wishlist/{item}/edition", data={"release": R2.upper(), "next": "/wishlist"}, headers=hx
    ).text
    assert "Edition: 2024-06-07 · Worldwide · Digital Media · 15 tracks" in chosen
    assert "Change edition" in chosen and f"release/{R2}" in chosen
    # its links come first under Where to buy: R2's Bandcamp before R1's Qobuz
    assert chosen.index("charlixcx.bandcamp.com") < chosen.index("qobuz.com/gb-en/album")

    back = client.post(f"/wishlist/{item}/edition", data={"release": "", "next": "/wishlist"})
    assert (back.status_code, back.headers["location"]) == (303, f"/wishlist#wish-{item}")
    assert "Choose edition" in client.get("/wishlist").text
    assert client.post(f"/wishlist/{item}/edition", data={"release": "nope"}).status_code == 400
    assert client.post(f"/wishlist/{item + 1}/edition", data={"release": ""}).status_code == 404
    assert client.get(f"/wishlist/{item + 1}/editions").status_code == 404
    assert len(opener.requests) == 1


def test_editions_errors_are_shown(tmp_path: Path, monkeypatch):
    client, _ = links_app(tmp_path, monkeypatch, 500)
    fragment = client.get(f"/wishlist/{item_id(tmp_path)}/editions", headers={"HX-Request": "1"})
    assert "MusicBrainz answered 500" in fragment.text


def test_album_search_results_have_no_edition_select(tmp_path: Path, monkeypatch):
    search_app(tmp_path, monkeypatch, RELEASE_GROUPS)
    page = TestClient(create_app(settings_for(tmp_path))).get("/search", params={"title": "brat"})
    assert "Add to wishlist" in page.text and "<select" not in page.text
