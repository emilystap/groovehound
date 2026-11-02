import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from groovehound import similar
from groovehound.brainz import listenbrainz
from groovehound.brainz.http import ServiceError, ServicePause
from groovehound.db import OWNER_ID, Follow, Store
from groovehound.web import create_app

from .conftest import FakeOpener, settings_for
from .test_app import search_app
from .test_brainz import lb_client, params
from .test_follows import BROWSE, store_at
from .test_search import ARTIST

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
BECK = "309c62ba-7a22-4277-9f67-4a162526d18a"
ARCADE_FIRE = "52074ba6-e495-4ef3-9bb4-0703888a9f68"
PORTISHEAD = "8f6bd1e4-fbe1-4f50-aa9b-94c450ec0f11"
NAMELESS = "8f6bd1e4-fbe1-4f50-aa9b-94c450ec0f12"


def recording(artist: str, name: str | None, count: int = 1) -> dict:
    """One recording in an LB Radio artist response, as ListenBrainz gives it (checked
    2026-11-02)."""
    found = {
        "recording_mbid": "f4308eb4-7f4f-480c-ae5f-216a562ffac7",
        "similar_artist_mbid": artist,
        "total_listen_count": count,
    }
    if name is not None:
        found["similar_artist_name"] = name
    return found


RADIO = {  # keyed by MBID, sorted: no similarity order; the asked-about artist among them
    BECK: [recording(BECK, "Beck", 358)],
    ARCADE_FIRE: [recording(ARCADE_FIRE, "Arcade Fire", 1953)],
    ARTIST: [recording(ARTIST, "Charli xcx")],
    PORTISHEAD: [recording(PORTISHEAD, "The Portishead")],
    NAMELESS: [recording(NAMELESS, None)],
    "not-an-mbid": [recording("x", "Nobody")],
}


def fetch(store: Store, opener: FakeOpener, pause: ServicePause | None = None, **kw) -> bool:
    return similar.fetch(
        store, lb_client(opener), pause or ServicePause(), ARTIST, now=kw.pop("now", NOW), **kw
    )


def test_one_request_cached_for_weeks(tmp_path: Path):
    opener = FakeOpener(RADIO, RADIO)
    with store_at(tmp_path) as store:
        assert similar.cached(store, OWNER_ID, ARTIST) is None
        assert fetch(store, opener, rng=random.Random(1)) is True
        (request,) = opener.requests
        assert request.full_url.split("?")[0].endswith(f"/1/lb-radio/artist/{ARTIST}")
        assert params(request) == {
            "mode": ["easy"],
            "max_similar_artists": [str(similar.COUNT)],
            "max_recordings_per_artist": ["1"],
            "pop_begin": ["0"],
            "pop_end": ["100"],
        }

        found = similar.cached(store, OWNER_ID, ARTIST, now=NOW)
        # by name, case aside; not the artist asked about, nor nameless or malformed keys
        assert [a.name for a in found.artists] == ["Arcade Fire", "Beck", "The Portishead"]
        assert found.fetched_at.startswith("2026-11-02") and not found.due

        later = NOW + similar.TTL[0] - timedelta(days=1)
        assert fetch(store, opener, now=later) is False  # still fresh: nothing asked
        assert len(opener.requests) == 1
        stale = NOW + similar.TTL[1]
        assert similar.cached(store, OWNER_ID, ARTIST, now=stale).due
        assert fetch(store, opener, now=stale) is True
        assert len(opener.requests) == 2


def test_a_leading_the_is_ignored_when_sorting():
    data = {BECK: [recording(BECK, "The Beta Band")], ARCADE_FIRE: [recording(ARCADE_FIRE, "Blur")]}
    assert [a.name for a in similar._artists(data, ARTIST)] == ["The Beta Band", "Blur"]


def test_dismissed_artists_are_counted_not_shown(tmp_path: Path):
    with store_at(tmp_path) as store:
        fetch(store, FakeOpener(RADIO))
        store.dismiss(OWNER_ID, similar.DISMISSED_KIND, BECK)
        found = similar.cached(store, OWNER_ID, ARTIST)
        assert [a.name for a in found.artists] == ["Arcade Fire", "The Portishead"]
        assert found.hidden == 1
        assert len(similar.cached(store, 2, ARTIST).artists) == 3  # per user


def test_not_found_is_cached_and_an_error_pauses(tmp_path: Path):
    pause = ServicePause()
    with store_at(tmp_path) as store:
        fetch(store, FakeOpener(404), pause)
        assert similar.cached(store, OWNER_ID, ARTIST).artists == ()
        assert pause.remaining() == 0
        assert fetch(store, FakeOpener()) is False  # the 404 counts as an answer

        other = FakeOpener(500)
        with pytest.raises(ServiceError):
            similar.fetch(store, lb_client(other), pause, BECK, now=NOW)
        assert pause.remaining() > 0
        with pytest.raises(ServiceError, match="paused"):
            similar.fetch(store, lb_client(FakeOpener()), pause, BECK, now=NOW)
        assert similar.cached(store, OWNER_ID, BECK) is None


# --- Web -------------------------------------------------------------------------------------


def radio_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *answers) -> FakeOpener:
    """An app whose ListenBrainz client answers from a FakeOpener (and MusicBrainz browses
    the artist once)."""
    search_app(tmp_path, monkeypatch, BROWSE)
    opener = FakeOpener(*answers)
    monkeypatch.setattr(listenbrainz, "client_for", lambda settings: lb_client(opener))
    return opener


def test_artist_page_asks_only_when_you_press_the_button(tmp_path: Path, monkeypatch):
    opener = radio_app(tmp_path, monkeypatch, RADIO)
    with store_at(tmp_path) as store:
        store.follow(OWNER_ID, Follow(BECK, "Beck"))
    client = TestClient(create_app(settings_for(tmp_path)))
    page = client.get(f"/artists/{ARTIST}").text
    assert "Find similar artists" in page and f'action="/artists/{ARTIST}/similar"' in page
    assert opener.requests == []

    card = client.post(
        f"/artists/{ARTIST}/similar", data={"name": "Charli xcx"}, headers={"HX-Request": "true"}
    ).text
    assert 'id="similar"' in card
    assert card.index("Arcade Fire") < card.index("Beck") < card.index("The Portishead")
    assert f'action="/artists/{ARCADE_FIRE}/follow"' in card
    assert f'action="/artists/{BECK}/unfollow"' in card  # followed already
    assert f'href="/artists/{PORTISHEAD}">The Portishead</a>' in card
    assert "Find similar artists" not in card and "Ask again" not in card
    assert len(opener.requests) == 1

    client.post(f"/artists/{ARTIST}/similar", headers={"HX-Request": "true"})
    assert len(opener.requests) == 1  # cached: pressing again asks nothing
    page = client.get(f"/artists/{ARTIST}").text
    assert "Arcade Fire" in page and "Find similar artists" not in page

    gone = client.post(f"/artists/{ARCADE_FIRE}/dismiss", headers={"HX-Request": "true"})
    assert gone.text == ""
    page = client.get(f"/artists/{ARTIST}").text
    assert "Arcade Fire" not in page and "1 dismissed" in page


def test_plain_form_posts_go_back(tmp_path: Path, monkeypatch):
    radio_app(tmp_path, monkeypatch, 404)
    client = TestClient(create_app(settings_for(tmp_path)), follow_redirects=False)
    back = client.post(f"/artists/{ARTIST}/similar")
    assert back.status_code == 303 and back.headers["location"] == f"/artists/{ARTIST}#similar"
    back = client.post(f"/artists/{BECK}/dismiss", data={"next": "https://evil.example"})
    assert back.status_code == 303 and back.headers["location"] == "/"
    assert client.post("/artists/not-an-mbid/similar").status_code == 404
    assert "doesn't know any artists like" in client.get(f"/artists/{ARTIST}").text


def test_errors_show_on_the_card(tmp_path: Path, monkeypatch):
    opener = radio_app(tmp_path, monkeypatch, 500)
    client = TestClient(create_app(settings_for(tmp_path)))
    card = client.post(f"/artists/{ARTIST}/similar", headers={"HX-Request": "true"}).text
    assert "follow-error" in card and "Find similar artists" in card
    assert len(opener.requests) == 1


def test_without_a_contact_nothing_is_asked(tmp_path: Path, monkeypatch):
    opener = radio_app(tmp_path, monkeypatch)
    from groovehound.config import Settings

    client = TestClient(create_app(Settings(database_path=tmp_path / "groovehound.db")))
    assert "Find similar artists" not in client.get(f"/artists/{ARTIST}").text
    card = client.post(f"/artists/{ARTIST}/similar", headers={"HX-Request": "true"}).text
    assert "follow-error" in card
    assert opener.requests == []
