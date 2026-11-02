from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from groovehound.config import Settings, load_settings
from groovehound.db import OWNER_ID, Store
from groovehound.web import create_app

from .conftest import settings_for


def test_settings_precedence(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text(
        '[brainz]\ncontact = "file@example.com"\n[listenbrainz]\nuser = "me"\ntoken = "t"\n'
    )
    settings = load_settings(config, env={"GROOVEHOUND_CONTACT": "env@example.com"})
    assert settings.contact == "env@example.com"
    assert settings.listenbrainz_user == "me"
    assert "t" not in repr(settings).split("listenbrainz_user")[1]


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


def test_home_explains_a_missing_contact(tmp_path: Path):
    client = TestClient(create_app(Settings(database_path=tmp_path / "db.sqlite")))
    assert "Not connected to MusicBrainz" in client.get("/").text


def test_bad_musicbrainz_settings_fail_at_startup(tmp_path: Path):
    with pytest.raises(ValueError):
        create_app(settings_for(tmp_path, musicbrainz_interval=0.5))
