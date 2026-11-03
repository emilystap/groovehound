from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from groovehound import covers_ahead, follows, library
from groovehound.brainz.http import ServicePause
from groovehound.db import OWNER_ID, Follow, Store
from groovehound.web import create_app

from .conftest import FakeOpener, settings_for
from .test_covers import caa_client, jpeg, picture
from .test_follow_types import SINGLE, SINGLES
from .test_follows import ARTIST, BROWSE, NOW, RG, RG_EP, RG_OLD
from .test_library import REL, flac

LATER = NOW + timedelta(days=61)  # past every cover's 30-60 days


def following(store: Store, *, singles: bool = False) -> None:
    types = ("album", "ep", "single") if singles else ("album", "ep")
    store.follow(
        OWNER_ID,
        Follow(ARTIST, "Charli XCX", followed_at=NOW.isoformat(), types=types,
               singles_since=NOW.isoformat() if singles else None),
    )  # fmt: skip
    store.save_cached("musicbrainz", follows.CACHE_KIND, ARTIST, BROWSE, NOW.isoformat())
    store.save_cached("musicbrainz", follows.SINGLES_KIND, ARTIST, SINGLES, NOW.isoformat())


def test_wanted_is_the_followed_types_newest_first(tmp_path: Path):
    with Store(tmp_path / "db.sqlite") as store:
        assert covers_ahead.wanted(store) == []
        following(store)
        # the live album isn't of the follow's types; undated last; singles only when on
        assert covers_ahead.wanted(store) == [RG, RG_OLD, RG_EP]
        store.set_follow_types(OWNER_ID, ARTIST, ("album", "ep", "single"), NOW.isoformat())
        assert SINGLE in covers_ahead.wanted(store)


def test_a_batch_fetches_due_covers_only_and_is_bounded(tmp_path: Path):
    opener = FakeOpener(jpeg(), jpeg(), jpeg(), 404)
    client = caa_client(opener)
    with Store(tmp_path / "db.sqlite") as store:
        following(store)
        first = covers_ahead.fetch_once(store, client, ServicePause(), batch=2, now=NOW)
        assert (first.made, first.left, first.error) == (2, 1, "")
        assert [r.full_url.split("/")[-2] for r in opener.requests] == [RG, RG_OLD]
        second = covers_ahead.fetch_once(store, client, ServicePause(), batch=2, now=NOW)
        assert (second.made, second.left) == (1, 0)
        assert covers_ahead.fetch_once(store, client, ServicePause(), now=NOW).made == 0
        assert len(opener.requests) == 3  # nothing due: nothing asked

        assert covers_ahead.fetch_once(store, client, ServicePause(), batch=1, now=LATER).made == 1
        assert len(opener.requests) == 4  # refreshed once the cached cover is due


def test_library_covers_need_no_request(tmp_path: Path):
    root = tmp_path / "music"
    flac(root / "B/01.flac", musicbrainz_albumid=REL, musicbrainz_releasegroupid=RG)
    (root / "B/cover.png").write_bytes(picture())
    opener = FakeOpener()
    with Store(tmp_path / "db.sqlite") as store:
        following(store)
        library.scan(store, OWNER_ID, root, now=NOW)
        result = covers_ahead.fetch_once(
            store, caa_client(opener), ServicePause(), library_root=root, batch=1, now=NOW
        )
        assert result.made == 1 and opener.requests == []
        assert store.cover(RG).source == "B/cover.png"


def test_an_error_stops_the_batch_and_pauses_the_archive(tmp_path: Path):
    opener = FakeOpener(jpeg(), 500, 500, 500)
    pause = ServicePause()
    with Store(tmp_path / "db.sqlite") as store:
        following(store)
        result = covers_ahead.fetch_once(store, caa_client(opener), pause, now=NOW)
        assert result.made == 1 and result.error and pause.remaining()
        asked = len(opener.requests)
        again = covers_ahead.fetch_once(store, caa_client(opener), pause, now=NOW)
        assert again.made == 0 and "paused" in again.error
        assert len(opener.requests) == asked


def test_the_app_runs_the_trickle_only_with_a_contact(tmp_path: Path, monkeypatch):
    started = []
    monkeypatch.setattr(covers_ahead.CoverFetcher, "start", lambda self: started.append(self))
    with TestClient(create_app(settings_for(tmp_path))):
        pass
    with TestClient(create_app(replace(settings_for(tmp_path), contact=""))):
        pass
    assert len(started) == 1
