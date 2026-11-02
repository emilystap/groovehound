import os
import struct
from datetime import UTC, datetime
from pathlib import Path

import mutagen
import pytest
from fastapi.testclient import TestClient
from mutagen.apev2 import APEv2
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TALB, TPE2, TXXX
from mutagen.mp4 import MP4FreeForm, MP4Tags

from groovehound import cli, library
from groovehound.config import load_settings
from groovehound.db import OWNER_ID, LibraryFile, Store
from groovehound.library import FileTags, LibraryScanner, scan, tags_from
from groovehound.web import create_app

from .conftest import settings_for

REL = "11111111-1111-1111-1111-111111111111"
REL2 = "22222222-2222-2222-2222-222222222222"
RG = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
RG2 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
AA = "cccccccc-cccc-cccc-cccc-cccccccccccc"
AA2 = "dddddddd-dddd-dddd-dddd-dddddddddddd"
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def flac(path: Path, **tags: str) -> Path:
    """A FLAC file with no audio, only STREAMINFO and these Vorbis comments."""
    info = (
        struct.pack(">HH", 4096, 4096)
        + b"\0" * 6
        + ((44100 << 44) | (1 << 41) | (15 << 36)).to_bytes(8, "big")
        + b"\0" * 16
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC\x80" + len(info).to_bytes(3, "big") + info)
    if tags:
        audio = FLAC(path)
        for key, value in tags.items():
            audio[key] = value
        audio.save()
    return path


def mp3(path: Path, *frames: object) -> Path:
    """An MP3 file of silent-ish MPEG frames with an ID3 tag of these frames."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((b"\xff\xfb\x90\x64" + b"\0" * 413) * 10)
    tag = ID3()
    for frame in frames:
        tag.add(frame)
    tag.save(path)
    return path


def album(root: Path, folder: str, release: str, tracks: int = 2, **extra: str) -> None:
    for n in range(1, tracks + 1):
        flac(
            root / folder / f"{n:02}.flac",
            musicbrainz_albumid=release,
            musicbrainz_releasegroupid=extra.get("rg", RG),
            musicbrainz_albumartistid=AA,
            album=extra.get("title", "Album"),
            albumartist="Artist",
        )


# --- Reading tags ------------------------------------------------------------------------


def test_reads_vorbis_comments_from_flac(tmp_path: Path):
    path = flac(
        tmp_path / "a.flac",
        MUSICBRAINZ_ALBUMID=REL,
        MUSICBRAINZ_RELEASEGROUPID=RG.upper(),
        MUSICBRAINZ_ALBUMARTISTID=AA,
        ALBUM="Blue",
        ALBUMARTIST="Joni Mitchell",
    )
    assert library.read_tags(path) == FileTags(REL, RG, (AA,), "Blue", "Joni Mitchell")


def test_reads_id3_txxx_from_mp3(tmp_path: Path):
    path = mp3(
        tmp_path / "a.mp3",
        TXXX(desc="MusicBrainz Album Id", text=[REL]),
        TXXX(desc="MusicBrainz Release Group Id", text=[RG]),
        # ID3v2.3 as Picard writes it: several ids in one string
        TXXX(desc="MusicBrainz Album Artist Id", text=[f"{AA}/{AA2}"]),
        TALB(text=["Blue"]),
        TPE2(text=["Joni Mitchell"]),
    )
    assert library.read_tags(path) == FileTags(REL, RG, (AA, AA2), "Blue", "Joni Mitchell")


def test_reads_mp4_freeform_atoms():
    tags = MP4Tags()
    tags["----:com.apple.iTunes:MusicBrainz Album Id"] = [MP4FreeForm(REL.encode())]
    tags["----:com.apple.iTunes:MusicBrainz Release Group Id"] = [MP4FreeForm(RG.encode())]
    tags["----:com.apple.iTunes:MusicBrainz Album Artist Id"] = [
        MP4FreeForm(AA.encode()),
        MP4FreeForm(AA2.encode()),
    ]
    tags["\xa9alb"] = ["Blue"]
    tags["aART"] = ["Joni Mitchell"]
    assert tags_from(tags) == FileTags(REL, RG, (AA, AA2), "Blue", "Joni Mitchell")


def test_reads_apev2(tmp_path: Path):
    tags = APEv2()
    tags["MUSICBRAINZ_ALBUMID"] = REL
    tags["MUSICBRAINZ_ALBUMARTISTID"] = f"{AA}\0{AA2}"
    tags["Album"] = "Blue"
    assert tags_from(tags) == FileTags(REL, None, (AA, AA2), "Blue", "")


def test_falls_back_to_track_artists_without_album_artist_tags():
    tags = {"musicbrainz_albumid": [REL], "musicbrainz_artistid": [AA], "artist": ["Solo"]}
    assert tags_from(tags) == FileTags(REL, None, (AA,), "", "Solo")


def test_untagged_and_not_audio(tmp_path: Path):
    assert library.read_tags(flac(tmp_path / "a.flac")) == FileTags()
    junk = tmp_path / "junk.flac"
    junk.write_bytes(b"not audio at all")
    with pytest.raises(mutagen.MutagenError):
        library.read_tags(junk)


# --- Scanning ----------------------------------------------------------------------------


def test_scan_builds_the_library_and_lists_untagged_files(tmp_path: Path):
    root = tmp_path / "music"
    album(root, "Artist/Album", REL)
    album(root, "Artist/Double/CD1", REL2, rg=RG2, title="Double")
    album(root, "Artist/Double/CD2", REL2, rg=RG2, title="Double")
    flac(root / "Loose/untagged.flac")
    (root / "Bad").mkdir()
    (root / "Bad/broken.mp3").write_bytes(b"nope")
    (root / "Artist/Album/cover.jpg").write_bytes(b"jpeg")  # not audio: not even counted
    flac(root / ".hidden/x.flac", musicbrainz_albumid=REL)

    with Store(tmp_path / "db.sqlite") as store:
        result = scan(store, OWNER_ID, root, now=NOW)
        assert (result.files, result.read, result.releases, result.error) == (8, 8, 2, "")
        found = {r.release_mbid: r for r in store.library(OWNER_ID)}
        assert found[REL].release_group_mbid == RG
        assert found[REL].album_artist_mbids == (AA,)
        assert (found[REL].title, found[REL].artist) == ("Album", "Artist")
        assert (found[REL2].folder, found[REL2].files) == ("Artist/Double", 4)
        assert found[REL].added_at == "2026-11-02T12:00:00+00:00"

        summary = store.library_summary(OWNER_ID)
        assert (summary.releases, summary.release_groups, summary.without_release_group) == (
            2,
            2,
            0,
        )
        assert (summary.files, summary.untagged, summary.unreadable) == (8, 1, 1)
        untagged = store.library_files(OWNER_ID, tagged=False)
        assert library.untagged_folders(untagged) == [("Loose", 1)]
        assert [f.path for f in untagged if f.problem] == ["Bad/broken.mp3"]

        scanned = store.library_scan(OWNER_ID)
        assert (scanned.files, scanned.read, scanned.error) == (8, 8, "")
        assert scanned.finished_at


def test_rescan_reads_only_changed_files_and_forgets_gone_ones(tmp_path: Path):
    root = tmp_path / "music"
    album(root, "A", REL)
    album(root, "B", REL2, rg=RG2)
    read: list[Path] = []

    def counting(path: Path) -> FileTags:
        read.append(path)
        return library.read_tags(path)

    with Store(tmp_path / "db.sqlite") as store:
        scan(store, OWNER_ID, root, now=NOW)
        assert scan(store, OWNER_ID, root, now=LATER, read=counting).read == 0
        assert read == []

        (root / "B/01.flac").unlink()
        (root / "B/02.flac").unlink()
        changed = root / "A/01.flac"
        audio = FLAC(changed)
        audio["album"] = "Renamed"
        audio.save()
        os.utime(changed, ns=(1, 10**18))  # a different mtime, whatever the clock's resolution
        result = scan(store, OWNER_ID, root, now=LATER, read=counting)
        assert (result.read, result.removed, result.releases) == (1, 2, 1)
        assert read == [changed]
        (kept,) = store.library(OWNER_ID)
        assert kept.release_mbid == REL
        assert kept.added_at == "2026-11-02T12:00:00+00:00"  # first found, not rescanned
        assert kept.last_seen == "2026-11-03T12:00:00+00:00"


def test_trickle_scan_reads_a_few_files_at_a_time(tmp_path: Path):
    root = tmp_path / "music"
    album(root, "A", REL, tracks=3)
    with Store(tmp_path / "db.sqlite") as store:
        first = scan(store, OWNER_ID, root, limit=2, now=NOW)
        assert (first.files, first.read, first.left) == (3, 2, 1)
        assert store.library_scan(OWNER_ID).left_over == 1
        second = scan(store, OWNER_ID, root, limit=2, now=LATER)
        assert (second.read, second.left) == (1, 0)
        assert store.library(OWNER_ID)[0].files == 3


def test_a_missing_or_empty_folder_changes_nothing(tmp_path: Path):
    """An unmounted share must not empty the library."""
    root = tmp_path / "music"
    album(root, "A", REL)
    with Store(tmp_path / "db.sqlite") as store:
        scan(store, OWNER_ID, root, now=NOW)
        missing = scan(store, OWNER_ID, tmp_path / "elsewhere", now=LATER)
        assert "isn't there" in missing.error
        for f in (root / "A").iterdir():
            f.unlink()
        empty = scan(store, OWNER_ID, root, now=LATER)
        assert "no audio files" in empty.error
        assert store.library_scan(OWNER_ID).error == empty.error
        assert len(store.library(OWNER_ID)) == 1
        assert len(store.library_index(OWNER_ID)) == 2


def test_library_releases_takes_what_most_files_say():
    files = [
        LibraryFile("X/1.flac", 1, 1, REL, RG, (AA,), "Title", "Artist"),
        LibraryFile("X/2.flac", 1, 1, REL, RG, (AA,), "Title", "Artist"),
        LibraryFile("X/3.flac", 1, 1, REL, None, (), "Title (bonus)", ""),
        LibraryFile("Y/1.flac", 1, 1),
    ]
    (release,) = library.library_releases(files)
    assert (release.release_group_mbid, release.title, release.files) == (RG, "Title", 3)
    assert release.folder == "X"


def test_scanner_requests_a_full_scan(tmp_path: Path):
    root = tmp_path / "music"
    album(root, "A", REL, tracks=3)
    scanner = LibraryScanner(settings_for(tmp_path, library_path=root), batch=1)
    assert scanner.run_once().read == 1  # a trickle
    assert scanner.run_once(full=True).read == 2


# --- Config, CLI and the page ------------------------------------------------------------


def test_library_path_from_config_and_env(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text('[library]\npath = "/music"\n')
    assert load_settings(config, env={}).library_path == Path("/music")
    env = {"GROOVEHOUND_LIBRARY": "/elsewhere"}
    assert load_settings(config, env=env).library_path == Path("/elsewhere")
    config.write_text("")
    assert load_settings(config, env={}).library_path is None


def test_cli_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.chdir(tmp_path)  # no ./config.toml
    monkeypatch.delenv("GROOVEHOUND_LIBRARY", raising=False)
    root = tmp_path / "music"
    album(root, "A", REL)
    db = tmp_path / "db.sqlite"
    assert cli.main(["--db", str(db), "scan"]) == 2  # no library configured
    monkeypatch.setenv("GROOVEHOUND_LIBRARY", str(root))
    assert cli.main(["--db", str(db), "scan"]) == 0
    assert "1 release(s)" in capsys.readouterr().out


def test_library_page_without_a_folder(tmp_path: Path):
    client = TestClient(create_app(settings_for(tmp_path)))
    page = client.get("/library").text
    assert "No music folder yet" in page
    assert client.post("/library/scan").status_code == 404


def test_library_page_and_rescan(tmp_path: Path):
    root = tmp_path / "music"
    album(root, "Artist/Album", REL)
    flac(root / "Loose/untagged.flac")
    settings = settings_for(tmp_path, library_path=root)
    app = create_app(settings)
    client = TestClient(app)
    assert "Not scanned yet" in client.get("/library").text

    status = client.post("/library/scan", headers={"HX-Request": "true"}).text
    assert 'hx-trigger="every 3s"' in status and "Scanning" in status
    assert app.state.scanner.scanning

    app.state.scanner.run_once(full=True)  # what the background thread would do
    app.state.scanner._asked = False
    page = client.get("/library").text
    assert "1 release (1 album) from 3 files" in " ".join(page.split())
    assert "Last scanned" in page and "every 3s" not in page
    assert "<code>Loose</code>" in page

    response = client.post("/library/scan", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/library"
