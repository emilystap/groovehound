import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from groovehound import covers
from groovehound.brainz import coverart
from groovehound.brainz.http import RateLimiter, ServiceError, ServicePause
from groovehound.db import OWNER_ID, Store, WishlistItem
from groovehound.web import create_app

from .conftest import CONTACT, FakeClock, FakeOpener, settings_for

RG = "c4bc9aa6-6fe6-4a1d-a9c3-3ad8a8e3a1a0"
JPEG = b"\xff\xd8\xff\xe0 a tiny jpeg"
NOW = datetime(2026, 11, 2, tzinfo=UTC)


def caa_client(opener: FakeOpener) -> coverart.CoverArtClient:
    clock = FakeClock()
    return coverart.CoverArtClient(
        limiter=RateLimiter(clock=clock, sleep=clock.sleep), contact=CONTACT, opener=opener
    )


def jpeg() -> tuple[bytes, dict[str, str]]:
    return JPEG, {"Content-Type": "image/jpeg"}


def test_fetched_once_then_from_the_cache_until_due(tmp_path: Path):
    opener = FakeOpener(jpeg(), jpeg())
    client, pause = caa_client(opener), ServicePause()
    with Store(tmp_path / "db.sqlite") as store:
        first = covers.cover(store, client, pause, RG, now=NOW, rng=random.Random(1))
        assert (first.image, first.content_type) == (JPEG, "image/jpeg")
        request = opener.requests[0]
        assert request.full_url == f"https://coverartarchive.org/release-group/{RG}/front-250"
        assert request.get_header("User-agent").endswith(f"( {CONTACT} )")
        assert request.get_header("Accept") == "image/*"

        later = NOW + covers.COVER_TTL[0] - timedelta(seconds=1)
        assert covers.cover(store, client, pause, RG, now=later).image == JPEG
        assert len(opener.requests) == 1
        covers.cover(store, client, pause, RG, now=NOW + covers.COVER_TTL[1])
        assert len(opener.requests) == 2


def test_no_cover_art_is_remembered(tmp_path: Path):
    opener = FakeOpener(404)
    client, pause = caa_client(opener), ServicePause()
    with Store(tmp_path / "db.sqlite") as store:
        assert covers.cover(store, client, pause, RG, now=NOW).image is None
        assert covers.cover(store, client, pause, RG, now=NOW).image is None
    assert len(opener.requests) == 1


def test_a_failure_keeps_the_stale_cover_and_pauses(tmp_path: Path):
    opener = FakeOpener(jpeg(), 500)
    client, pause = caa_client(opener), ServicePause()
    due = NOW + covers.COVER_TTL[1]
    with Store(tmp_path / "db.sqlite") as store:
        covers.cover(store, client, pause, RG, now=NOW)
        assert covers.cover(store, client, pause, RG, now=due).image == JPEG
        assert pause.remaining() > 0
        assert covers.cover(store, client, pause, RG, now=due).image == JPEG
        assert len(opener.requests) == 2  # none while paused
        # nothing cached: the error is the caller's
        with pytest.raises(ServiceError, match="paused"):
            covers.cover(store, client, pause, RG.replace("c", "d"), now=NOW)
        with pytest.raises(ServiceError, match="contact"):
            covers.cover(store, None, ServicePause(), RG.replace("c", "d"), now=NOW)


@pytest.mark.parametrize(
    "answer",
    [
        (b"<script>alert(1)</script>", {"Content-Type": "text/html"}),
        (b"x" * (covers.MAX_BYTES + 1), {"Content-Type": "image/jpeg"}),
        (b"", {"Content-Type": "image/png"}),
    ],
)
def test_only_reasonable_images_are_kept(tmp_path: Path, answer):
    client = caa_client(FakeOpener(answer))
    with Store(tmp_path / "db.sqlite") as store:
        with pytest.raises(ServiceError, match="Unexpected cover"):
            covers.cover(store, client, ServicePause(), RG, now=NOW)
        assert store.cover(RG) is None


def cover_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *answers) -> FakeOpener:
    opener = FakeOpener(*answers)
    monkeypatch.setattr(coverart, "client_for", lambda settings: caa_client(opener))
    return opener


def test_cover_route_serves_wishlist_items_only(tmp_path: Path, monkeypatch):
    opener = cover_app(tmp_path, monkeypatch, jpeg())
    client = TestClient(create_app(settings_for(tmp_path)))
    assert client.get(f"/cover/{RG}").status_code == 404  # not on the wishlist
    assert client.get("/cover/not-an-mbid").status_code == 404
    assert opener.requests == []

    with Store(tmp_path / "groovehound.db") as store:
        store.add_to_wishlist(
            OWNER_ID, WishlistItem(release_group_mbid=RG, title="T", artist_credit="A")
        )
    for _ in range(2):
        response = client.get(f"/cover/{RG.upper()}")
        assert response.content == JPEG
        assert response.headers["content-type"] == "image/jpeg"
        assert "max-age=2592000" in response.headers["cache-control"]
        assert response.headers["x-content-type-options"] == "nosniff"
    assert len(opener.requests) == 1
    assert f'src="/cover/{RG}"' in client.get("/wishlist").text
    assert f'src="/cover/{RG}"' in client.get("/").text


def test_cover_route_without_art_or_when_failing(tmp_path: Path, monkeypatch):
    other = RG.replace("c", "d")
    cover_app(tmp_path, monkeypatch, 404, 500)
    client = TestClient(create_app(settings_for(tmp_path)))
    with Store(tmp_path / "groovehound.db") as store:
        store.add_to_wishlist(
            OWNER_ID, WishlistItem(release_group_mbid=RG, title="T", artist_credit="A")
        )
        store.add_to_wishlist(
            OWNER_ID, WishlistItem(release_group_mbid=other, title="U", artist_credit="A")
        )
    none = client.get(f"/cover/{RG}")
    assert (none.status_code, none.headers["cache-control"]) == (404, "max-age=86400")
    failed = client.get(f"/cover/{other}")
    assert (failed.status_code, failed.headers["cache-control"]) == (503, "no-store")
