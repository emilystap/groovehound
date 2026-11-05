from pathlib import Path

from fastapi.testclient import TestClient

from groovehound import follows, library_artists
from groovehound.brainz.http import ServicePause
from groovehound.db import OWNER_ID, LibraryRelease, Store
from groovehound.web import create_app

from .conftest import FakeOpener, settings_for
from .test_app import search_app
from .test_brainz import mb_client, params
from .test_follow_types import SINGLE, SINGLES
from .test_follows import BROWSE, NOW, RG_EP, RG_OLD, VARIOUS
from .test_search import ARTIST, GUEST, RG, RG_COMP

DEFAULT = ("album", "ep")
RG_BOOT = "0a1b2c3d-0000-4000-8000-0000000000d1"  # a bootleg: not in the official browse
RG_GUEST = [f"0a1b2c3d-0000-4000-8000-0000000000e{n}" for n in range(3)]
FOUND_AT = "2026-10-30T09:00:00+00:00"


def release(n: int, group: str | None, artists: tuple[str, ...], title: str, credit: str):
    mbid = f"0a1b2c3d-0000-4000-8000-0000000001{n:02d}"
    return LibraryRelease(mbid, group, artists, title, credit, title, 10)


LIBRARY = [
    release(1, RG, (ARTIST,), "Brat", "Charli xcx"),
    release(2, RG, (ARTIST,), "Brat", "Charli xcx"),  # another edition: one release group
    release(3, RG_BOOT, (ARTIST,), "Live in Leeds", "Charli xcx"),
    release(4, "0a1b2c3d-0000-4000-8000-0000000000f1", (VARIOUS,), "Now 99", "Various Artists"),
    *(release(5 + n, g, (GUEST,), f"Guest {n}", "The Guest") for n, g in enumerate(RG_GUEST)),
    release(9, None, (GUEST, ARTIST), "Duets", "The Guest & Charli xcx"),  # no release group id
]


def owning(store: Store) -> None:
    store.replace_library(OWNER_ID, LIBRARY, FOUND_AT)


def test_library_artists_without_requests(tmp_path: Path):
    with Store(tmp_path / "groovehound.db") as store:
        owning(store)
        assert library_artists.count(store, OWNER_ID) == 2  # Various Artists isn't anyone's
        found = library_artists.library_artists(store, OWNER_ID, DEFAULT)
        # named as the files have them (alone, if they ever are), "The" aside when sorting
        assert [(a.name, a.owned, a.gaps) for a in found] == [
            ("Charli xcx", 3, None),  # Brat, the bootleg, Duets: not checked yet
            ("The Guest", 4, None),
        ]
        assert [a.name for a in library_artists.suggestions(store, OWNER_ID, found)] == [
            "The Guest",
            "Charli xcx",
        ]
        store.dismiss(OWNER_ID, library_artists.DISMISSED_KIND, GUEST)
        assert [a.mbid for a in library_artists.suggestions(store, OWNER_ID, found)] == [ARTIST]


def test_what_is_missing_once_the_browse_is_cached(tmp_path: Path):
    opener = FakeOpener(BROWSE)
    with Store(tmp_path / "groovehound.db") as store:
        owning(store)
        follows.discography(store, mb_client(opener), ServicePause(), ARTIST, now=NOW)
        (charli, _) = library_artists.library_artists(store, OWNER_ID, DEFAULT)
        assert charli.sort_name == "Charli xcx"  # from the browse's credits now
        gaps = charli.gaps
        # owned whatever its type; missing only albums and EPs: the live album isn't counted
        assert gaps.owned == {RG, RG_BOOT, LIBRARY[-1].release_mbid}
        assert gaps.missing == {RG_OLD, RG_EP}
        assert (gaps.have, gaps.total) == (3, 5)
        assert [r.title for r in gaps.also] == ["Duets", "Live in Leeds"]

        # counting live albums too: that one's missing as well
        found, _ = follows.cached_discography(store, ARTIST)
        live = library_artists.gaps(store, OWNER_ID, ARTIST, found, (*DEFAULT, "live"))
        assert live.missing == {RG_OLD, RG_EP, RG_COMP}
        only = library_artists.missing_only(found, live)
        assert {h.mbid for h in only.release_groups} == {RG_OLD, RG_EP, RG_COMP}
        assert only.singles is None
        # with singles counted but not browsed, the list can't say yet
        (charli, _) = library_artists.library_artists(store, OWNER_ID, (*DEFAULT, "single"))
        assert charli.gaps is None
        # compilations under Various Artists aren't theirs
        assert library_artists.gaps(store, OWNER_ID, VARIOUS, found, DEFAULT) is None
        assert len(opener.requests) == 1


def test_following_leaves_suggestions_and_owned_albums_alone(tmp_path: Path):
    with Store(tmp_path / "groovehound.db") as store:
        owning(store)
        follows.follow(store, mb_client(FakeOpener(BROWSE)), ServicePause(), OWNER_ID, ARTIST)
        # owned but not in the browse: not marked seen, so it's new if MusicBrainz lists it later
        assert RG_BOOT not in store.release_groups_seen(ARTIST)
        found = library_artists.library_artists(store, OWNER_ID, DEFAULT)
        assert [a.following for a in found] == [True, False]
        assert [a.mbid for a in library_artists.suggestions(store, OWNER_ID, found)] == [GUEST]


def test_library_tab_and_suggestions(tmp_path: Path, monkeypatch):
    opener = search_app(tmp_path, monkeypatch)
    client = TestClient(create_app(settings_for(tmp_path)))
    with Store(tmp_path / "groovehound.db") as store:
        owning(store)
    assert 'In your library <span class="muted">(2)</span>' in client.get("/artists").text
    page = " ".join(client.get("/artists/library").text.split())
    assert "Artists to follow?" in page and f'id="suggested-follow-{GUEST}"' in page
    assert "3 in your library · not checked yet" in page
    assert f'href="/artists/{ARTIST}"' in page and "Now 99" not in page

    gone = client.post(f"/artists/{GUEST}/dismiss-suggestion", headers={"HX-Request": "true"})
    assert gone.text == ""
    page = client.get("/artists/library").text
    assert f'id="suggested-follow-{GUEST}"' not in page
    assert f'id="suggested-follow-{ARTIST}"' in page
    assert opener.requests == []


def test_artist_page_has_and_missing(tmp_path: Path, monkeypatch):
    opener = search_app(tmp_path, monkeypatch, BROWSE)
    client = TestClient(create_app(settings_for(tmp_path)))
    with Store(tmp_path / "groovehound.db") as store:
        owning(store)
    page = " ".join(client.get(f"/artists/{ARTIST}").text.split())
    assert "You have 3 of 5" in page and "Missing only · 2" in page
    assert "Also in your library (2)" in page and "Live in Leeds" in page

    page = client.get(f"/artists/{ARTIST}?missing=1").text
    assert "Missing albums and EPs" in page and "True Romance" in page
    assert "Brat" not in page and "Live at Glastonbury" not in page
    assert "Also in your library" not in page
    assert len(opener.requests) == 1  # the second view used the cached browse

    page = " ".join(client.get("/artists/library").text.split())
    assert "You have 3 of 5 · 2 missing" in page


def test_artist_page_browses_singles_when_they_count(tmp_path: Path, monkeypatch):
    opener = search_app(tmp_path, monkeypatch, BROWSE, SINGLES)
    settings = settings_for(tmp_path, follow_types=("album", "ep", "single"))
    client = TestClient(create_app(settings))
    with Store(tmp_path / "groovehound.db") as store:
        owning(store)
    page = " ".join(client.get(f"/artists/{ARTIST}").text.split())
    assert "You have 3 of 6" in page  # the single is missing too
    assert params(opener.requests[1])["type"] == ["single"]
    page = client.get(f"/artists/{ARTIST}?missing=1").text
    assert SINGLE in page
    assert len(opener.requests) == 2


def test_artist_page_for_someone_not_in_the_library(tmp_path: Path, monkeypatch):
    opener = search_app(tmp_path, monkeypatch, BROWSE)
    client = TestClient(create_app(settings_for(tmp_path, follow_types=("album", "single"))))
    page = client.get(f"/artists/{ARTIST}?missing=1").text
    assert "You have" not in page and "Brat" in page  # nothing to compare: shown whole
    assert len(opener.requests) == 1  # and no singles browse for them
