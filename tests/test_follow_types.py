from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from groovehound import follows
from groovehound.brainz.http import ServiceError, ServicePause
from groovehound.config import load_settings
from groovehound.db import OWNER_ID, Follow, Store
from groovehound.search import Hit
from groovehound.web import create_app

from .conftest import FakeOpener, settings_for
from .test_app import search_app
from .test_brainz import mb_client, params
from .test_follows import ARTIST, BROWSE, NOW, group

SINGLE = "0a1b2c3d-0000-4000-8000-000000000003"
SINGLES = {
    "release-group-count": 1,
    "release-groups": [{**group(SINGLE, "Von dutch", "2024-02-29"), "primary-type": "Single"}],
}


def hit(primary: str, *secondary: str) -> Hit:
    return Hit("x", "t", "a", primary_type=primary, secondary_types=secondary)


def store_at(tmp_path: Path) -> Store:
    return Store(tmp_path / "groovehound.db")


def following(store: Store, opener: FakeOpener) -> Follow:
    return follows.follow(store, mb_client(opener), ServicePause(), OWNER_ID, ARTIST, now=NOW)


def test_a_follow_is_for_albums_and_eps_by_default(tmp_path: Path):
    with store_at(tmp_path) as store:
        follow = following(store, FakeOpener(BROWSE))
    assert follow.types == ("album", "ep") and follow.singles_since is None
    assert follows.wanted(follow.types, hit("Album"))
    assert follows.wanted(follow.types, hit("EP"))
    assert not follows.wanted(follow.types, hit("Single"))
    assert not follows.wanted(follow.types, hit("Album", "Live"))
    assert not follows.wanted(follow.types, hit(""))


def test_secondary_types_must_all_be_wanted():
    types = ("album", "live", "other")
    assert follows.wanted(types, hit("Album", "Live"))
    assert follows.wanted(types, hit("Album", "Audio drama"))  # one of the rarer: "other"
    assert not follows.wanted(types, hit("Album", "Live", "Compilation"))


def test_labels_and_cleaning():
    assert follows.types_label(("album", "ep")) == "Albums, EPs"
    assert (
        follows.types_label(("album", "single", "live", "other")) == "Albums, Singles + Live, more"
    )
    assert follows.clean_types(["live", "single", "album"]) == ("album", "single", "live")
    with pytest.raises(ValueError, match="at least one"):
        follows.clean_types(["live"])
    with pytest.raises(ValueError, match="unknown"):
        follows.clean_types(["album", "bootleg"])


def test_new_means_first_seen_after_following():
    follow = Follow(ARTIST, "Charli xcx", followed_at=NOW.isoformat())
    before, after = ((NOW + timedelta(days=d)).isoformat() for d in (-1, 1))
    assert follows.is_new(follow, hit("Album"), after)
    assert not follows.is_new(follow, hit("Album"), before)
    assert not follows.is_new(follow, hit("Album", "Live"), after)  # not followed for live
    assert not follows.is_new(follow, hit("Single"), after)  # nor singles


def test_turning_singles_on_browses_them_as_already_out(tmp_path: Path):
    opener = FakeOpener(BROWSE, SINGLES)
    later = NOW + timedelta(days=2)
    with store_at(tmp_path) as store:
        following(store, opener)
        client, pause = mb_client(opener), ServicePause()
        follow = follows.set_types(
            store, client, pause, OWNER_ID, ARTIST, ("album", "single"), now=later
        )
        assert params(opener.requests[1])["type"] == ["single"]
        assert follow.types == ("album", "single")
        assert follow.singles_since == later.isoformat()
        first_seen = store.release_groups_seen(ARTIST)[SINGLE]
        assert not follows.is_new(follow, hit("Single"), first_seen)  # already out
        newer = later + timedelta(days=1)
        assert follows.is_new(follow, hit("Single"), newer.isoformat())
        assert follows.cached_discography(store, ARTIST, now=later)[0].singles[0].mbid == SINGLE

        # other types change nothing about singles, and ask nothing
        follow = follows.set_types(
            store, client, pause, OWNER_ID, ARTIST, ("album", "single", "live"), now=newer
        )
        assert follow.singles_since == later.isoformat()
        # off clears the date; on again starts it over, from the cached browse
        off = follows.set_types(store, client, pause, OWNER_ID, ARTIST, ("album",), now=newer)
        assert off.singles_since is None
        on = follows.set_types(store, client, pause, OWNER_ID, ARTIST, ("single",), now=newer)
        assert on.singles_since == newer.isoformat()
    assert len(opener.requests) == 2


def test_a_failed_singles_browse_leaves_the_follow_alone(tmp_path: Path):
    opener = FakeOpener(BROWSE, 500, 500, 500)
    with store_at(tmp_path) as store:
        following(store, opener)
        with pytest.raises(ServiceError):
            follows.set_types(
                store, mb_client(opener), ServicePause(), OWNER_ID, ARTIST, ("album", "single")
            )
        follow = store.follow_of(OWNER_ID, ARTIST)
    assert follow.types == ("album", "ep") and follow.singles_since is None


def test_following_with_singles_by_default_browses_them_as_already_out(tmp_path: Path):
    opener = FakeOpener(BROWSE, SINGLES)
    types = ("album", "ep", "single")
    with store_at(tmp_path) as store:
        client, pause = mb_client(opener), ServicePause()
        follow = follows.follow(store, client, pause, OWNER_ID, ARTIST, types, now=NOW)
        assert params(opener.requests[1])["type"] == ["single"]
        assert follow.types == types and follow.singles_since == NOW.isoformat()
        first_seen = store.release_groups_seen(ARTIST)[SINGLE]
        assert not follows.is_new(follow, hit("Single"), first_seen)
        # already following: its types are kept, nothing asked
        again = follows.follow(store, client, pause, OWNER_ID, ARTIST, ("album",), now=NOW)
        assert again.types == types
    assert len(opener.requests) == 2


def test_a_failed_singles_browse_follows_no_one(tmp_path: Path):
    opener = FakeOpener(BROWSE, 500, 500, 500)
    with store_at(tmp_path) as store:
        with pytest.raises(ServiceError):
            follows.follow(
                store, mb_client(opener), ServicePause(), OWNER_ID, ARTIST, ("album", "single")
            )
        assert store.follow_of(OWNER_ID, ARTIST) is None


def test_set_types_for_someone_not_followed(tmp_path: Path):
    opener = FakeOpener()
    with store_at(tmp_path) as store:
        assert (
            follows.set_types(store, mb_client(opener), ServicePause(), OWNER_ID, ARTIST, ("ep",))
            is None
        )
    assert opener.requests == []


# --- Web -------------------------------------------------------------------------------------


def test_types_form_on_the_artist_pages(tmp_path: Path, monkeypatch):
    opener = search_app(tmp_path, monkeypatch, BROWSE, SINGLES)
    client = TestClient(create_app(settings_for(tmp_path)))
    page = client.get(f"/artists/{ARTIST}").text
    assert f'action="/artists/{ARTIST}/types"' not in page  # not following yet

    # following on the artist page brings the form with it; on other pages it doesn't
    here = f"/artists/{ARTIST}"
    hx = {"HX-Request": "true"}
    followed = client.post(f"/artists/{ARTIST}/follow", data={"next": here}, headers=hx).text
    assert f'id="follow-types-{ARTIST}" hx-swap-oob="true"' in followed
    assert "New releases: Albums, EPs" in followed
    gone = client.post(f"/artists/{ARTIST}/unfollow", data={"next": "/search"}, headers=hx).text
    assert "follow-types" not in gone
    client.post(f"/artists/{ARTIST}/follow")
    assert "New releases: Albums, EPs ·" in client.get("/artists").text

    # none of albums, EPs and singles: refused, nothing changed
    refused = client.post(
        f"/artists/{ARTIST}/types", data={"type": ["live"], "next": here}, headers=hx
    )
    assert "at least one" in refused.text and "New releases: Albums, EPs" in refused.text

    saved = client.post(
        f"/artists/{ARTIST}/types",
        data={"type": ["album", "ep", "single"], "next": here},
        headers=hx,
    )
    assert saved.headers["HX-Refresh"] == "true"  # the page reloads to list the singles
    assert "New releases: Albums, EPs, Singles" in saved.text and "Saved." in saved.text
    page = client.get(here).text
    assert "Singles (1)" in page and "Von dutch" in page
    assert len(opener.requests) == 2

    again = client.post(
        f"/artists/{ARTIST}/types",
        data={"type": ["album", "single", "live"], "next": here},
        headers=hx,
    )
    assert "HX-Refresh" not in again.headers
    assert "New releases: Albums, Singles + Live" in again.text


def test_follow_types_from_the_config(tmp_path: Path, monkeypatch):
    opener = search_app(tmp_path, monkeypatch, BROWSE, SINGLES)
    settings = settings_for(tmp_path, follow_types=("album", "ep", "single"))
    client = TestClient(create_app(settings))
    followed = client.post(
        f"/artists/{ARTIST}/follow", data={"next": "/artists"}, headers={"HX-Request": "true"}
    )
    assert "New releases: Albums, EPs, Singles ·" in " ".join(followed.text.split())
    assert len(opener.requests) == 2
    with pytest.raises(ValueError, match="at least one"):
        create_app(settings_for(tmp_path, follow_types=("live",)))
    with pytest.raises(ValueError, match="unknown"):
        create_app(settings_for(tmp_path, follow_types=("album", "singles")))


def test_follow_types_in_toml_and_env(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text('[follows]\ntypes = ["Album", "single"]\n', encoding="utf-8")
    assert load_settings(config, env={}).follow_types == ("album", "single")
    env = {"GROOVEHOUND_FOLLOW_TYPES": "album, ep ,single"}
    assert load_settings(config, env=env).follow_types == ("album", "ep", "single")
    config.write_text("", encoding="utf-8")
    assert load_settings(config, env={}).follow_types == ("album", "ep")


def test_types_without_javascript_and_when_not_following(tmp_path: Path, monkeypatch):
    search_app(tmp_path, monkeypatch, BROWSE)
    client = TestClient(create_app(settings_for(tmp_path)), follow_redirects=False)
    assert client.post(f"/artists/{ARTIST}/types", data={"type": "ep"}).status_code == 404
    client.post(f"/artists/{ARTIST}/follow")
    back = client.post(f"/artists/{ARTIST}/types", data={"type": "ep", "next": "/artists"})
    assert (back.status_code, back.headers["location"]) == (303, "/artists")
    with store_at(tmp_path) as store:
        assert store.follow_of(OWNER_ID, ARTIST).types == ("ep",)
