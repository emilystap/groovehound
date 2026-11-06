from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

from fastapi.testclient import TestClient

from groovehound import library_albums, shelves
from groovehound.db import OWNER_ID, LibraryRelease, Listen, Store, WishlistItem
from groovehound.library_albums import NEVER_PLAYED, NOT_LATELY, PARTIAL, WISHLIST, Album, Library
from groovehound.tracks import Partial
from groovehound.web import create_app

from .conftest import FakeOpener, settings_for
from .test_listens import USER, entry, fetch, stats

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
TODAY = "2026-11-02"
ARTIST = "0a1b2c3d-0000-4000-8000-000000000a01"
MBIDS = [f"0a1b2c3d-0000-4000-8000-00000000{n:04d}" for n in range(1, 8)]
FRESH, STALE, UNHEARD, PART, *_ = MBIDS


def release(mbid: str, title: str, artist: str, *, tracks=None, files: int = 2):
    return LibraryRelease(
        "1" + mbid[1:],
        mbid,
        (ARTIST,),
        title,
        artist,
        f"{artist}/{title}",
        files,
        tracks=tracks,
    )


def setup(store: Store, *, history: bool = True) -> None:
    """Four albums: played last week, played a year ago, never played, and partly there."""
    store.replace_library(
        OWNER_ID,
        [
            release(FRESH, "Fresh", "The Abbots"),
            release(STALE, "Stale", "Beta"),
            release(UNHEARD, "Unheard", "Gamma"),
            release(PART, "Part", "Delta", tracks=12, files=8),
        ],
        "2026-10-30T09:00:00+00:00",
    )
    played = stats(entry(FRESH, 5), entry(STALE, 3), entry(PART, 1))
    fetch(store, FakeOpener(played))
    if history:
        store.start_listen_history(OWNER_ID, USER.lower(), NOW.isoformat())
        ago = {FRESH: 7, STALE: 365}
        store.save_listens(
            OWNER_ID,
            [
                Listen(int((NOW - timedelta(days=d)).timestamp()), f"msid-{d}", None, mbid)
                for mbid, d in ago.items()
            ],
        )
        store.listen_fill_progress(OWNER_ID, 1, None, NOW.isoformat())


def test_the_filters(tmp_path: Path):
    with Store(tmp_path / "db.sqlite") as store:
        setup(store)
        store.add_to_wishlist(OWNER_ID, WishlistItem(FRESH, "Fresh", "The Abbots"))
        found = library_albums.library(store, OWNER_ID, USER, now=NOW)
    assert [a.title for a in library_albums.chosen(found)] == ["Fresh", "Stale", "Part", "Unheard"]
    assert [a.title for a in library_albums.chosen(found, NEVER_PLAYED)] == ["Unheard"]
    assert [a.title for a in library_albums.chosen(found, PARTIAL)] == ["Part"]
    assert {a.title for a in library_albums.chosen(found, NOT_LATELY)} == {"Stale", "Part"}
    assert [a.title for a in library_albums.chosen(found, WISHLIST)] == ["Fresh"]
    assert [(c.key, c.count) for c in library_albums.filters(found)] == [
        (NEVER_PLAYED, 1),
        (PARTIAL, 1),
        (NOT_LATELY, 2),
        (WISHLIST, 1),
    ]


def test_nothing_is_called_unplayed_or_not_lately_on_a_guess(tmp_path: Path):
    with Store(tmp_path / "db.sqlite") as store:
        setup(store, history=False)
        found = library_albums.library(store, OWNER_ID, USER, now=NOW)
        assert found.never_played and not found.not_lately  # the history has the dates
        assert library_albums.known_filter(found, NOT_LATELY) == ""
        assert library_albums.known_filter(found, NEVER_PLAYED) == NEVER_PLAYED

        nobody = library_albums.library(store, OWNER_ID, "", now=NOW)
    assert not nobody.never_played and not nobody.not_lately
    assert library_albums.known_filter(nobody, NEVER_PLAYED) == ""
    assert [c.key for c in library_albums.filters(nobody)] == [PARTIAL]


def album(n: int, title: str, artist: str, *, added="", listens=0, played="") -> Album:
    return Album(MBIDS[n], title, artist, ARTIST, (ARTIST,), added, listens, played)


LIBRARY = Library(
    albums=(
        album(0, "Zed", "The Abbots", added="2026-01-01", listens=2, played="2026-10-01"),
        album(1, "Alpha", "Beta", added="2026-03-01", listens=1),  # before the history
        album(2, "Moon", "Gamma", added="2026-02-01", listens=4, played="2026-02-01"),
        album(3, "Never", "Delta", added="2026-04-01"),
    ),
    never_played=frozenset({MBIDS[3]}),
    partial={MBIDS[0]: Partial("r", 1, 4), MBIDS[2]: Partial("r", 3, 4)},
    not_lately=frozenset(),
    wishlist=frozenset(),
)


def titles(filter_key: str, sort: str) -> list[str]:
    return [a.title for a in library_albums.chosen(LIBRARY, filter_key, sort)]


def test_the_sorts():
    assert titles("", "artist") == ["Zed", "Alpha", "Never", "Moon"]  # "The" aside
    assert titles("", "title") == ["Alpha", "Moon", "Never", "Zed"]
    assert titles("", "added") == ["Never", "Alpha", "Moon", "Zed"]
    # the longest ago first: undated (before the history), then dated, then never played
    assert titles("", "played") == ["Alpha", "Moon", "Zed", "Never"]
    assert titles(PARTIAL, "covered") == ["Moon", "Zed"]


def test_each_filter_has_its_own_order():
    assert titles(PARTIAL, "") == ["Moon", "Zed"]  # best covered
    assert library_albums.known_sort(NEVER_PLAYED, "") == "mixed"
    assert library_albums.known_sort(NOT_LATELY, "") == "played"
    assert library_albums.known_sort("", "nonsense") == "artist"
    assert library_albums.known_sort("", "covered") == "artist"  # only with Partial


def test_mixed_spreads_each_artists_albums_for_the_day():
    many = [album(0, f"Many {n}", "Many") for n in range(8)]
    many = [replace(a, mbid=f"many-{n}", artist_key=("many",)) for n, a in enumerate(many)]
    others = [
        replace(album(1, f"Other {n}", f"Other {n}"), mbid=f"other-{n}", artist_key=(f"o{n}",))
        for n in range(24)
    ]
    shown = [*many, *others]
    order = library_albums.mixed(shown, "2026-11-05")
    assert {a.mbid for a in order} == {a.mbid for a in shown}
    assert order == library_albums.mixed(shown, "2026-11-05")  # the pages join up
    assert order != library_albums.mixed(shown, "2026-11-06")
    places = [n for n, a in enumerate(order) if a.artist == "Many"]
    assert min(b - a for a, b in pairwise(places)) > 1  # never side by side


def test_urls_stay_short():
    assert library_albums.url() == "/library/albums"
    assert library_albums.url(PARTIAL, "covered") == "/library/albums?filter=partial"
    assert library_albums.url(PARTIAL, "title") == "/library/albums?filter=partial&sort=title"
    assert library_albums.url("", "added", 2) == "/library/albums?sort=added&page=2"


def test_a_page_of_the_grid(monkeypatch):
    monkeypatch.setattr(library_albums, "PAGE", 3)
    first = library_albums.page(LIBRARY)
    assert [a.title for a in first.albums] == ["Zed", "Alpha", "Never"]
    assert (first.total, first.of) == (4, 4)
    assert first.next_url == "/library/albums?page=2"
    assert first.sort.label == "By artist"

    last = library_albums.page(LIBRARY, number=2)
    assert [a.title for a in last.albums] == ["Moon"] and not last.next_url
    assert library_albums.page(LIBRARY, number=-1).albums == first.albums

    chips = library_albums.page(LIBRARY, PARTIAL).chips
    assert [(c.label, c.on) for c in chips] == [("Never played · 1", False), ("Partial · 2", True)]
    assert chips[1].url == "/library/albums"  # pressing it again takes it off
    assert library_albums.page(LIBRARY, "nonsense", "nonsense").filter_key == ""

    empty = replace(LIBRARY, partial={})  # a bookmark with nothing in it says so
    assert library_albums.page(empty, PARTIAL).total == 0


def test_a_library_tile_only_says_what_wants_attention():
    def badge(n: int, filter_key: str = "") -> str:
        tiles = shelves.library_tiles([LIBRARY.albums[n]], LIBRARY, TODAY, filter_key)
        assert tiles[0].quiet and tiles[0].hit is None and not tiles[0].dismiss
        return tiles[0].badge

    assert badge(0) == "1 of 4 tracks"
    assert badge(1) == ""
    assert badge(3) == "Never played"
    assert badge(3, NEVER_PLAYED) == ""  # the page says so already
    assert badge(1, NOT_LATELY) == "Not played lately"  # played, before the history begins
    assert shelves.library_tiles([LIBRARY.albums[0]], LIBRARY, TODAY)[0].credits[0].mbid == ARTIST


def test_the_shelves_open_the_grid_with_their_filter():
    found = shelves.library(LIBRARY, TODAY)  # nothing in Not played lately: left out
    assert [(s.title, s.see_all) for s in found] == [
        ("Never played", "/library/albums?filter=never-played"),
        ("Partial", "/library/albums?filter=partial"),
    ]
    assert [t.title for t in found[1].tiles] == ["Moon", "Zed"]


def test_the_pages_without_a_request(tmp_path: Path):
    settings = settings_for(tmp_path, listenbrainz_user=USER)
    with Store(settings.database_path) as store:
        setup(store)
    client = TestClient(create_app(settings))

    page = client.get("/library").text
    assert '<section class="shelf" id="never-played">' in page
    assert "All albums" in page and "4 albums" in page
    assert f'<a href="/artists/{ARTIST}">Gamma</a>' in page  # the name opens the artist
    assert 'href="/library/albums?filter=never-played"' in page
    assert "In your library</span>" not in page  # they all are
    assert f'href="/albums/{PART}"' in page and "8 of 12 tracks" in page

    page = client.get("/library/albums?filter=partial").text
    assert "1 of 4" in page and "Best covered" in page and "Unheard" not in page
    assert 'aria-label="Back to Library"' in page
    assert 'aria-current="page">Partial · 1' in page

    assert "By artist (" not in client.get("/library/albums").text


def test_scrolling_down_gets_the_next_tiles_alone(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(library_albums, "PAGE", 3)
    settings = settings_for(tmp_path, listenbrainz_user=USER)
    with Store(settings.database_path) as store:
        setup(store)
    client = TestClient(create_app(settings))
    page = client.get("/library/albums").text
    assert 'hx-get="/library/albums?page=2" hx-trigger="revealed"' in page

    more = client.get("/library/albums?page=2", headers={"HX-Request": "true"}).text
    assert more.lstrip().startswith("<li") and "<html" not in more
    assert "Unheard" in more and "revealed" not in more


def test_follow_in_a_tiles_menu_comes_back_to_the_grid(tmp_path: Path):
    settings = settings_for(tmp_path, listenbrainz_user=USER)
    with Store(settings.database_path) as store:
        setup(store)
    client = TestClient(create_app(settings))
    page = client.get("/library/albums?filter=partial&sort=title").text
    assert 'value="/library/albums?filter=partial&amp;sort=title"' in page
    assert f'action="/artists/{ARTIST}/follow"' in page


def test_scan_points_at_what_it_could_not_use(tmp_path: Path):
    settings = settings_for(tmp_path)
    with Store(settings.database_path) as store:
        store.replace_library(
            OWNER_ID,
            [LibraryRelease(MBIDS[4], None, (ARTIST,), "Loose", "Delta", "Loose", 2)],
            "2026-10-30T09:00:00+00:00",
        )
    client = TestClient(create_app(settings))
    assert 'aria-label="Scan: 1 thing to look at"' in client.get("/library").text
    assert "Without a release group (1)" in client.get("/library/scan").text
