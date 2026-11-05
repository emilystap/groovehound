import re
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from groovehound import new_releases, rediscover
from groovehound.db import OWNER_ID, LibraryRelease, Listen, Store
from groovehound.follows import NOT_FOLLOWABLE
from groovehound.web import create_app

from .conftest import FakeOpener, settings_for
from .test_library import FOUND_AT
from .test_listens import USER, entry, fetch, stats
from .test_new_releases import NOW, RG_NEW, RG_RECENT, followed
from .test_search import ARTIST

VARIOUS = next(iter(sorted(NOT_FOLLOWABLE)))
ARTISTS = {
    name: f"0a1b2c3d-0000-4000-8000-0000000005{i:02d}" for i, name in enumerate("BCD", start=1)
}


def rg(n: int) -> str:
    return f"0a1b2c3d-0000-4000-8000-0000000006{n:02d}"


def album(n: int, artist: str, name: str) -> LibraryRelease:
    return LibraryRelease(f"0a1b2c3d-0000-4000-8000-0000000007{n:02d}", rg(n), (artist,),
                          f"Album {n}", name, f"Album {n}", 2)  # fmt: skip


# Charli (ARTIST) plays most; Various Artists has more listens but isn't an artist
LIBRARY = [
    album(1, ARTIST, "Charli xcx"),  # 100 listens, yesterday
    album(2, ARTIST, "Charli xcx"),  # never played
    album(3, ARTIST, "Charli xcx"),  # not in the stats yet, but in the history: played
    album(4, ARTIST, "Charli xcx"),  # 2 listens, 8 months ago
    album(11, ARTISTS["B"], "B"),  # 10 listens, a year ago
    album(12, ARTISTS["B"], "B"),  # never played
    album(21, ARTISTS["C"], "C"),  # 5 listens, none in the history: before it
    album(31, ARTISTS["D"], "D"),  # 1 listen, 10 days ago
    album(41, VARIOUS, "Various Artists"),  # 500 listens, none in the history
    album(42, VARIOUS, "Various Artists"),  # never played
]
STATS = stats(
    entry(rg(1), 100), entry(rg(4), 2), entry(rg(11), 10), entry(rg(21), 5), entry(rg(31), 1),
    entry(rg(41), 500),
)  # fmt: skip


def ago(**delta) -> int:
    return int((NOW - timedelta(**delta)).timestamp())


HISTORY = [
    Listen(ago(days=1), "m1", release_group_mbid=rg(1)),
    Listen(ago(days=2), "m3", release_group_mbid=rg(3)),
    Listen(ago(days=240), "m4", release_group_mbid=rg(4)),
    Listen(ago(days=365), "m11", release_group_mbid=rg(11)),
    Listen(ago(days=10), "m31", release_group_mbid=rg(31)),
    Listen(ago(days=3), "m-new", release_group_mbid=RG_RECENT),  # a new release already played
]


@pytest.fixture(autouse=True)
def draw_everything(monkeypatch):
    for section in ("FAVOURITES", "OTHERS", "NOT_LATELY", "NEW"):
        monkeypatch.setattr(rediscover, section, 100)


def set_up(store: Store, *, filled: bool = True) -> None:
    store.replace_library(OWNER_ID, LIBRARY, FOUND_AT)
    fetch(store, FakeOpener(STATS), now=NOW)
    store.start_listen_history(OWNER_ID, USER.lower(), FOUND_AT)
    store.save_listens(OWNER_ID, HISTORY)
    store.listen_fill_progress(OWNER_ID, 1, None, FOUND_AT if filled else None)


def mbids(picks) -> set[str]:
    return {p.mbid for p in picks}


def draw(store: Store, **kwargs):
    return rediscover.draw(store, OWNER_ID, USER, now=NOW, seed="00000001", **kwargs)


def test_sections(tmp_path: Path):
    with Store(tmp_path / "db.sqlite") as store:
        set_up(store)
        followed(store)
        found = draw(store)
    assert mbids(found.favourites) == {rg(2)}
    assert mbids(found.others) == {rg(12), rg(42)}  # Various Artists is never a favourite
    assert mbids(found.not_lately) == {rg(4), rg(11), rg(21), rg(41)}
    assert found.history_ready
    picks = {p.mbid: p for p in found.picks}
    assert picks[rg(4)].listens == 2 and picks[rg(4)].last_played.startswith("2026-03")
    assert picks[rg(21)].last_played == ""  # played before the history
    assert picks[rg(2)].artist_mbid == ARTIST and picks[rg(42)].artist_mbid == ""


def test_new_releases_by_artists_played_most_not_yet_played(tmp_path: Path):
    with Store(tmp_path / "db.sqlite") as store:
        set_up(store)
        followed(store)
        feed = {r.hit.mbid for r in new_releases.feed(store, OWNER_ID, now=NOW).new}
        found = draw(store)
    assert RG_NEW in feed and RG_RECENT in feed
    assert mbids(found.new) == feed - {RG_RECENT}  # RG_RECENT was played
    assert all(p.hit and p.artist_mbid == ARTIST for p in found.new)


def test_proportions(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(rediscover, "NOT_LATELY", 2)
    with Store(tmp_path / "db.sqlite") as store:
        set_up(store)
        first = draw(store)
        assert len(first.not_lately) == 2
        assert {len(draw(store).not_lately) for _ in range(3)} == {2}


def test_a_seed_draws_the_same_albums_and_a_larger_size_only_adds(tmp_path: Path):
    with Store(tmp_path / "db.sqlite") as store:
        set_up(store)
        whole = draw(store, per_section=100).not_lately
        assert len(whole) == 4
        assert draw(store, per_section=2).not_lately == whole[:2]
        assert draw(store, per_section=100).not_lately == whole

        store.dismiss(OWNER_ID, rediscover.DISMISSED_KIND, whole[0].mbid)
        assert draw(store, per_section=100).not_lately == whole[1:]  # the rest stay where they were


def test_a_seed_that_isnt_one_gets_a_fresh_draw(tmp_path: Path):
    with Store(tmp_path / "db.sqlite") as store:
        set_up(store)
        found = rediscover.draw(store, OWNER_ID, USER, now=NOW, seed="<nope>")
    assert re.fullmatch("[0-9a-f]{8}", found.seed)


def test_dismissed_and_unfilled_history(tmp_path: Path):
    with Store(tmp_path / "db.sqlite") as store:
        set_up(store, filled=False)
        store.dismiss(OWNER_ID, rediscover.DISMISSED_KIND, rg(12))
        found = draw(store)
    assert not found.not_lately and not found.history_ready  # "lately" isn't known yet
    assert mbids(found.others) == {rg(42)}


def test_nothing_without_stats(tmp_path: Path):
    with Store(tmp_path / "db.sqlite") as store:
        store.replace_library(OWNER_ID, LIBRARY, FOUND_AT)
        assert draw(store) is None
        assert rediscover.draw(store, OWNER_ID, "", now=NOW) is None


def test_the_page(tmp_path: Path):
    settings = settings_for(tmp_path, listenbrainz_user=USER)
    with Store(settings.database_path) as store:
        set_up(store)
    client = TestClient(create_app(settings))
    page = " ".join(client.get("/rediscover").text.split())
    assert "Never played, by artists you play most" in page and "Album 2" in page
    assert "Not played in six months" in page and "2 listens · last" in page
    assert "5 listens · none since" in page
    assert "New from artists you play most" not in page  # nothing followed
    assert '<a href="/" aria-current="page">' in page  # Rediscover belongs to Home
    assert '<a class="button" href="/rediscover">Shuffle</a>' in page

    response = client.post(
        f"/rediscover/{rg(2)}/dismiss", data={"next": "/rediscover"}, follow_redirects=False
    )
    assert response.status_code == 303 and response.headers["location"] == "/rediscover"
    assert "Album 2<" not in client.get("/rediscover").text
    assert client.post(f"/rediscover/{rg(2)}/dismiss", headers={"HX-Request": "1"}).text == ""
    assert client.post("/rediscover/nope/dismiss").status_code == 404


def test_the_page_without_a_user_or_stats(tmp_path: Path):
    page = TestClient(create_app(settings_for(tmp_path))).get("/rediscover").text
    assert "Set your ListenBrainz username" in page
    page = TestClient(create_app(settings_for(tmp_path, listenbrainz_user=USER)))
    assert "Waiting for your ListenBrainz stats" in page.get("/rediscover").text
