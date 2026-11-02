import base64
import io
import os
import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader
from mutagen.apev2 import BINARY, APEv2, APEValue
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3
from mutagen.mp4 import MP4Cover
from PIL import Image

from groovehound import covers, library
from groovehound.brainz import coverart
from groovehound.brainz.http import RateLimiter, ServiceError, ServicePause
from groovehound.db import OWNER_ID, Store, WishlistItem
from groovehound.web import create_app

from .conftest import CONTACT, FakeClock, FakeOpener, settings_for
from .test_library import flac

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


# --- Covers from the library (roadmap 5i) ------------------------------------------------

REL = "11111111-1111-1111-1111-111111111111"


def picture(size: tuple[int, int] = (600, 400), color: str = "red", kind: str = "PNG") -> bytes:
    out = io.BytesIO()
    Image.new("RGBA" if kind == "PNG" else "RGB", size, color).save(out, kind)
    return out.getvalue()


def in_library(store: Store, root: Path, folder: str = "Artist/Album", **pictures: bytes) -> Path:
    """A two-track album of this release group in the library, scanned; `pictures` are files
    in its folder by name."""
    for n in (1, 2):
        flac(
            root / folder / f"0{n}.flac",
            musicbrainz_albumid=REL,
            musicbrainz_releasegroupid=RG,
            album="Album",
        )
    for name, data in pictures.items():
        (root / folder / name.replace("_", ".")).write_bytes(data)
    library.scan(store, OWNER_ID, root, now=NOW)
    return root / folder


def embed(path: Path, *pictures: tuple[int, bytes]) -> None:
    audio = FLAC(path)
    for kind, data in pictures:
        art = Picture()
        art.type, art.mime, art.data = kind, "image/png", data
        audio.add_picture(art)
    audio.save()


def size_of(image: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(image)) as opened:
        assert opened.format == "JPEG"
        return opened.size


def test_a_cover_beside_the_music_needs_no_request(tmp_path: Path):
    root = tmp_path / "music"
    with Store(tmp_path / "db.sqlite") as store:
        folder = in_library(store, root, folder_jpg=picture(color="blue"), Cover_PNG=picture())
        found = covers.cover(store, None, ServicePause(), RG, library_root=root, now=NOW)
        assert found.content_type == "image/jpeg"
        assert size_of(found.image) == (250, 167)
        assert found.source == "Artist/Album/Cover.PNG"  # cover.* before folder.*
        assert store.cover(RG).source == found.source

        # served from the cache while the file is unchanged...
        again = covers.cover(store, None, ServicePause(), RG, library_root=root, now=NOW)
        assert again.fetched_at == found.fetched_at
        # ...and made again when it changes
        (folder / "Cover.PNG").write_bytes(picture((100, 100)))
        os.utime(folder / "Cover.PNG", ns=(1, 1))
        changed = covers.cover(store, None, ServicePause(), RG, library_root=root, now=NOW)
        assert size_of(changed.image) == (100, 100)  # never enlarged


def test_embedded_art_the_front_cover_first(tmp_path: Path):
    root = tmp_path / "music"
    with Store(tmp_path / "db.sqlite") as store:
        folder = in_library(store, root)
        back, front = picture((300, 300), "green"), picture((500, 250), "red")
        embed(folder / "01.flac", (4, back), (library.FRONT_COVER, front))
        found = covers.cover(store, None, ServicePause(), RG, library_root=root, now=NOW)
        assert found.source == "Artist/Album/01.flac"
        assert size_of(found.image) == (250, 125)  # the front cover, not the back


def test_the_archive_only_without_art_in_the_library(tmp_path: Path):
    root = tmp_path / "music"
    opener = FakeOpener(jpeg(), jpeg())
    client, pause = caa_client(opener), ServicePause()
    with Store(tmp_path / "db.sqlite") as store:
        folder = in_library(store, root, cover_jpg=b"not an image")  # unreadable: skipped
        found = covers.cover(store, client, pause, RG, library_root=root, now=NOW)
        assert (found.image, found.source) == (JPEG, "")
        assert len(opener.requests) == 1
        covers.cover(store, client, pause, RG, library_root=root, now=NOW)
        assert len(opener.requests) == 1  # cached

        # art added to the library replaces the archive's at once
        (folder / "front.png").write_bytes(picture())
        found = covers.cover(store, client, pause, RG, library_root=root, now=NOW)
        assert found.source == "Artist/Album/front.png"

        # and when it's gone, the archive is asked again
        (folder / "front.png").unlink()
        found = covers.cover(store, client, pause, RG, library_root=root, now=NOW)
        assert (found.image, found.source) == (JPEG, "")
        assert len(opener.requests) == 2


def test_disc_folders_and_other_users(tmp_path: Path):
    root = tmp_path / "music"
    with Store(tmp_path / "db.sqlite") as store:
        for disc in ("CD1", "CD2"):
            flac(
                root / "Album" / disc / "01.flac",
                musicbrainz_albumid=REL,
                musicbrainz_releasegroupid=RG,
            )
        (root / "Album" / "CD2" / "folder.jpg").write_bytes(picture(kind="JPEG"))
        library.scan(store, OWNER_ID, root, now=NOW)
        found = covers.cover(store, None, ServicePause(), RG, library_root=root, now=NOW)
        assert found.source == "Album/CD2/folder.jpg"
        assert list(library.cover_art(store, OWNER_ID + 1, root, RG)) == []


def test_pictures_from_other_formats(tmp_path: Path):
    art = picture((10, 10))
    id3 = ID3()
    id3.add(APIC(type=3, mime="image/png", data=art))
    vorbis = Picture()
    vorbis.type, vorbis.data = library.FRONT_COVER, art
    block = base64.b64encode(vorbis.write()).decode()
    ape = APEv2()
    ape["Cover Art (Front)"] = APEValue(b"cover.png\0" + art, BINARY)
    for tags in (id3, {"metadata_block_picture": [block]}, {"covr": [MP4Cover(art)]}, ape):
        audio = SimpleNamespace(tags=tags)
        assert [data for _, data in library._pictures(audio)] == [art]


def test_cover_route_for_the_library_without_a_contact(tmp_path: Path, monkeypatch):
    root = tmp_path / "music"
    opener = cover_app(tmp_path, monkeypatch)
    settings = replace(settings_for(tmp_path, library_path=root), contact="")
    with Store(settings.database_path) as store:
        in_library(store, root, cover_jpg=picture(kind="JPEG"))
    client = TestClient(create_app(settings))
    response = client.get(f"/cover/{RG}")
    assert response.status_code == 200
    assert size_of(response.content) == (250, 167)
    assert opener.requests == []


def test_covers_of_owned_release_groups_come_from_the_cache():
    templates = Environment(loader=FileSystemLoader(Path(covers.__file__).parent / "web/templates"))
    template = templates.get_template("_cover.html")
    assert "coverartarchive.org" in template.render(mbid=RG)
    assert f'src="/cover/{RG}"' in template.render(mbid=RG, owned={RG: "2026-11-02"})
    assert f'src="/cover/{RG}"' in template.render(mbid=RG, cached=True)
