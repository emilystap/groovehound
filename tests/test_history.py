from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from groovehound import history, listens
from groovehound.brainz.http import ServiceError, ServicePause
from groovehound.db import OWNER_ID, AlbumPlays, LibraryRelease, Store
from groovehound.web import create_app

from .conftest import FakeOpener, settings_for
from .test_brainz import lb_client, params
from .test_follows import store_at
from .test_library import FOUND_AT
from .test_listens import R_CRASH, RG_CRASH, RG_OTHER, STATS, USER
from .test_search import ARTIST

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
T = 1793600000  # 2026-11-02, a little before NOW
RG_TAGS = "0a1b2c3d-0000-4000-8000-0000000000d1"  # the album the scrobbler sent
RG_MAPPED = "0a1b2c3d-0000-4000-8000-0000000000d2"  # ListenBrainz's match
REC = "0a1b2c3d-0000-4000-8000-0000000000d3"
REC_SENT = "0a1b2c3d-0000-4000-8000-0000000000d4"


def listen(
    at: float,
    msid: str = "",
    *,
    sent: str | None = None,
    mapped: str | None = None,
    recording: str | None = REC,
) -> dict:
    """A listen as the API returns it (shape checked 2026-11-02)."""
    info = {"submission_client": "navidrome", "recording_mbid": REC_SENT}
    if sent:
        info["release_group_mbid"] = sent
    meta: dict = {"artist_name": "Charli xcx", "track_name": "360", "additional_info": info}
    if recording or mapped:
        meta["mbid_mapping"] = {"recording_mbid": recording, "release_group_mbid": mapped}
    return {
        "inserted_at": int(at) + 5,
        "listened_at": at,
        "recording_msid": msid or f"msid-{int(at)}",
        "track_metadata": meta,
        "user_name": USER,
    }


def page(*listens: dict, oldest: int | None = None) -> dict:
    return {
        "payload": {
            "count": len(listens),
            "listens": list(listens),
            "latest_listen_ts": T,
            "oldest_listen_ts": oldest,
            "user_id": USER,
        }
    }


def fetch(store: Store, opener: FakeOpener, pause: ServicePause | None = None, now=NOW, user=USER):
    return history.fetch(store, lb_client(opener), pause or ServicePause(), OWNER_ID, user, now=now)


@pytest.fixture
def small_pages(monkeypatch):
    """Pages of 2 listens instead of 1,000."""
    monkeypatch.setattr(history, "MAX_LISTENS", 2)


def test_a_short_history_is_filled_in_one_request(tmp_path: Path):
    opener = FakeOpener(
        page(
            listen(T, sent=RG_TAGS, mapped=RG_MAPPED),  # the scrobbler's album wins
            listen(T - 60, mapped=RG_MAPPED),
            listen(T - 120, recording=None),  # not linked: kept, not matched
            {"listened_at": T - 180},  # no msid: not a listen ListenBrainz can name
            oldest=T - 120,
        )
    )
    with store_at(tmp_path) as store:
        assert fetch(store, opener) == 3
        request = opener.requests[0]
        assert request.full_url.startswith("https://api.listenbrainz.org/1/user/Rob/listens")
        assert params(request) == {"count": ["1000"]}  # from the newest
        assert store.album_plays(OWNER_ID) == {
            RG_TAGS: AlbumPlays(1, T),
            RG_MAPPED: AlbumPlays(1, T - 60),
        }
        status = history.status(store, OWNER_ID, "rob")  # the key ignores case
        assert status == history.Status(3, 2, "2026-11-02T06:11:20+00:00", False, False)
        assert fetch(store, opener, now=NOW + timedelta(hours=21)) is None  # not due
    assert len(opener.requests) == 1


def test_the_recording_is_listenbrainzs_match_else_the_scrobblers():
    [matched, sent, bad] = history.parse(
        [
            listen(T),
            listen(T - 1, recording=None, mapped=RG_MAPPED),
            {**listen(T - 2.0, recording=None), "track_metadata": {"additional_info": "x"}},
        ]
    )
    assert matched.recording_mbid == REC
    assert sent.recording_mbid == REC_SENT and sent.release_group_mbid == RG_MAPPED
    assert (bad.listened_at, bad.recording_mbid) == (T - 2, None)  # floats, as in an export


def test_a_scrobblers_id_that_isnt_an_mbid_is_ignored():
    [found] = history.parse([listen(T, sent="not-an-mbid", mapped=RG_MAPPED.upper())])
    assert found.release_group_mbid == RG_MAPPED


def test_the_fill_pages_back_a_few_requests_per_wake_up(tmp_path: Path, small_pages):
    opener = FakeOpener(
        page(listen(T), listen(T - 10)),
        page(listen(T - 10), listen(T - 20)),  # max_ts + 1: the boundary listen again, once
        page(listen(T - 30), listen(T - 40)),
        page(listen(T - 50), oldest=T - 50),
    )
    with store_at(tmp_path) as store:
        assert fetch(store, opener) == 5
        assert [params(r).get("max_ts") for r in opener.requests] == [
            None,
            [str(T - 9)],
            [str(T - 19)],
        ]
        assert history.status(store, OWNER_ID, USER).filling
        assert history.due(store, USER, now=NOW)  # the next wake-up goes on
        assert fetch(store, opener) == 1
        assert params(opener.requests[3])["max_ts"] == [str(T - 39)]
        assert not history.status(store, OWNER_ID, USER).filling
        assert not history.due(store, USER, now=NOW + timedelta(hours=21))


def test_the_fill_stops_at_ten_pages(tmp_path: Path, small_pages, monkeypatch):
    monkeypatch.setattr(history, "PAGES_PER_RUN", 20)
    opener = FakeOpener(*(page(listen(T - 2 * i), listen(T - 2 * i - 1)) for i in range(10)))
    with store_at(tmp_path) as store:
        assert fetch(store, opener) == 20
        status = history.status(store, OWNER_ID, USER)
        assert status.capped and not status.filling
    assert len(opener.requests) == history.MAX_PAGES


def test_top_ups_look_back_a_week_and_count_each_listen_once(tmp_path: Path):
    opener = FakeOpener(
        page(listen(T, sent=RG_TAGS), oldest=T),
        page(listen(T + 600, sent=RG_TAGS), listen(T, sent=RG_TAGS)),
    )
    with store_at(tmp_path) as store:
        fetch(store, opener)
        assert fetch(store, opener, now=NOW + timedelta(hours=27)) == 1
        assert params(opener.requests[1]) == {
            "count": ["1000"],
            "min_ts": [str(T - history.OVERLAP)],
        }
        assert store.album_plays(OWNER_ID) == {RG_TAGS: AlbumPlays(2, T + 600)}
        assert not history.due(store, USER, now=NOW + timedelta(hours=27 + 21))


def test_a_full_top_up_is_followed_by_the_next_page(tmp_path: Path, small_pages):
    opener = FakeOpener(
        page(listen(T), oldest=T),
        page(listen(T + 20), listen(T + 10)),  # oldest after min_ts first, newest first
        page(listen(T + 30)),
    )
    with store_at(tmp_path) as store:
        fetch(store, opener)
        assert fetch(store, opener, now=NOW + timedelta(hours=27)) == 3
        assert params(opener.requests[2])["min_ts"] == [str(T + 19)]


def test_a_changed_username_starts_again(tmp_path: Path):
    opener = FakeOpener(page(listen(T), oldest=T), page(listen(T - 5), oldest=T - 5))
    with store_at(tmp_path) as store:
        fetch(store, opener)
        assert fetch(store, opener, user="someone-else") == 1
        assert store.listen_summary(OWNER_ID).listens == 1
        assert history.status(store, OWNER_ID, USER) is None
        assert history.status(store, OWNER_ID, "someone-else").listens == 1


def test_an_unknown_user_is_remembered_without_a_pause(tmp_path: Path):
    opener = FakeOpener(404)
    pause = ServicePause()
    with store_at(tmp_path) as store:
        assert fetch(store, opener, pause) == 0
        assert not pause.remaining()
        assert history.status(store, OWNER_ID, USER) == history.Status(0, 0, "", False, False)
        assert fetch(store, opener, pause, now=NOW + timedelta(hours=21)) is None


def test_an_error_pauses_listenbrainz_and_keeps_the_pages_fetched(tmp_path: Path, small_pages):
    opener = FakeOpener(page(listen(T), listen(T - 10)), 500)
    pause = ServicePause()
    with store_at(tmp_path) as store:
        with pytest.raises(ServiceError):
            fetch(store, opener, pause)
        assert pause.remaining()
        assert store.listen_summary(OWNER_ID).listens == 2
        assert store.listen_history(OWNER_ID).before == T - 9  # goes on from there
        with pytest.raises(ServiceError, match="paused"):
            fetch(store, opener, pause)
    assert len(opener.requests) == 2


def test_nothing_asked_without_a_username(tmp_path: Path):
    opener = FakeOpener()
    with store_at(tmp_path) as store:
        assert fetch(store, opener, user=" ") is None
        assert history.status(store, OWNER_ID, "") is None
    assert opener.requests == []


def test_the_library_page_says_how_much_history_is_kept(tmp_path: Path):
    settings = settings_for(tmp_path, listenbrainz_user=USER)
    with Store(settings.database_path) as store:
        store.replace_library(
            OWNER_ID,
            [LibraryRelease(R_CRASH, RG_CRASH, (ARTIST,), "Crash", "Charli xcx", "Crash", 2)],
            FOUND_AT,
        )
        lb = lb_client(FakeOpener(STATS, page(listen(T, sent=RG_OTHER), listen(T - 60))))
        listens.fetch(store, lb, ServicePause(), OWNER_ID, USER, now=NOW)
        history.fetch(store, lb, ServicePause(), OWNER_ID, USER, now=NOW)
    page_text = " ".join(TestClient(create_app(settings)).get("/library").text.split())
    assert "Listen history: 2 listens since" in page_text
    assert "1 matched to an album." in page_text
