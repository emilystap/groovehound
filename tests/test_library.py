import os
import sqlite3
import struct
from datetime import UTC, datetime
from pathlib import Path

import mutagen
import pytest
from fastapi.testclient import TestClient
from mutagen.apev2 import APEv2
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TALB, TIT2, TMED, TPE1, TPE2, TPOS, TRCK, TSST, TXXX, UFID
from mutagen.mp4 import MP4FreeForm, MP4Tags

from groovehound import cli, library
from groovehound.config import load_settings
from groovehound.db import (
    MIGRATIONS,
    OWNER_ID,
    LibraryFile,
    LibraryRelease,
    Store,
    WishlistItem,
)
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
            tracknumber=str(n),
            tracktotal=str(tracks),
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
    assert library.read_tags(path) == FileTags(
        REL, RG, (AA, AA2), "Blue", "Joni Mitchell", length_ms=261
    )


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
    assert tags_from(tags) == FileTags(REL, None, (AA,), "", "Solo", artist="Solo")


REC = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
TRK = "ffffffff-ffff-ffff-ffff-ffffffffffff"


def test_reads_recording_and_release_track_ids(tmp_path: Path):
    """Roadmap 5k: Picard's *MusicBrainz Track Id* is the recording; ID3 keeps it in UFID."""
    vorbis = flac(
        tmp_path / "a.flac", MUSICBRAINZ_ALBUMID=REL, MUSICBRAINZ_TRACKID=REC,
        MUSICBRAINZ_RELEASETRACKID=TRK,
    )  # fmt: skip
    tags = library.read_tags(vorbis)
    assert (tags.recording_mbid, tags.track_mbid) == (REC, TRK)

    id3 = mp3(
        tmp_path / "a.mp3",
        TXXX(desc="MusicBrainz Album Id", text=[REL]),
        UFID(owner="http://musicbrainz.org", data=REC.encode()),
        TXXX(desc="MusicBrainz Release Track Id", text=[TRK]),
    )
    tags = library.read_tags(id3)
    assert (tags.recording_mbid, tags.track_mbid) == (REC, TRK)

    mp4 = MP4Tags()
    mp4["----:com.apple.iTunes:MusicBrainz Track Id"] = [MP4FreeForm(REC.encode())]
    assert tags_from(mp4).recording_mbid == REC
    assert (
        library.read_tags(flac(tmp_path / "b.flac", MUSICBRAINZ_ALBUMID=REL)).recording_mbid is None
    )


def test_tracks_are_kept_and_files_read_again_after_the_migration(tmp_path: Path):
    root = tmp_path / "music"
    flac(root / "A/01.flac", musicbrainz_albumid=REL, musicbrainz_trackid=REC)
    path = tmp_path / "db.sqlite"
    old = sqlite3.connect(path)  # a database from before 5k, with the file scanned
    for index, script in enumerate(MIGRATIONS[:12], start=1):
        old.executescript(script)
        old.execute(f"PRAGMA user_version = {index}")
    stat = (root / "A/01.flac").stat()
    old.execute(
        "INSERT INTO library_files (user_id, path, mtime_ns, size, release_mbid) "
        "VALUES (1, 'A/01.flac', ?, ?, ?)",
        (stat.st_mtime_ns, stat.st_size, REL),
    )
    old.commit()
    old.close()
    with Store(path) as store:
        assert scan(store, OWNER_ID, root, now=NOW).read == 1
        (file,) = store.library_files(OWNER_ID)
        assert file.recording_mbid == REC
        assert store.owned_recordings(OWNER_ID, [REC, TRK]) == {REC}


def test_reads_track_tags_for_local_track_lists(tmp_path: Path):
    """Title, track artist, track and disc numbers with totals, medium and disc title."""
    want = FileTags(
        REL, album_artist="Artist", title="Song", artist="Artist", track_number=3,
        track_total=12, disc_number=1, disc_total=2, media="CD", disc_title="Bonus",
    )  # fmt: skip
    vorbis = flac(
        tmp_path / "a.flac", MUSICBRAINZ_ALBUMID=REL, TITLE="Song", ARTIST="Artist",
        TRACKNUMBER="3", TRACKTOTAL="12", DISCNUMBER="1", TOTALDISCS="2", MEDIA="CD",
        DISCSUBTITLE="Bonus",
    )  # fmt: skip
    assert library.read_tags(vorbis) == want
    id3 = mp3(
        tmp_path / "a.mp3",
        TXXX(desc="MusicBrainz Album Id", text=[REL]),
        TIT2(text=["Song"]),
        TPE1(text=["Artist"]),
        TRCK(text=["3/12"]),
        TPOS(text=["1/2"]),
        TMED(text=["CD"]),
        TSST(text=["Bonus"]),
    )
    assert library.read_tags(id3) == FileTags(**{**want.__dict__, "length_ms": 261})

    mp4 = MP4Tags()
    mp4["----:com.apple.iTunes:MusicBrainz Album Id"] = [MP4FreeForm(REL.encode())]
    mp4["©nam"] = ["Song"]
    mp4["©ART"] = ["Artist"]
    mp4["trkn"] = [(3, 12)]
    mp4["disk"] = [(1, 2)]
    mp4["----:com.apple.iTunes:MEDIA"] = [MP4FreeForm(b"CD")]
    mp4["----:com.apple.iTunes:DISCSUBTITLE"] = [MP4FreeForm(b"Bonus")]
    assert tags_from(mp4) == want

    ape = APEv2()
    ape["MUSICBRAINZ_ALBUMID"] = REL
    for key, value in [("Title", "Song"), ("Artist", "Artist"), ("Track", "3/12"),
                       ("Disc", "1/2"), ("Media", "CD"), ("DiscSubtitle", "Bonus")]:  # fmt: skip
        ape[key] = value
    assert tags_from(ape) == want

    # MP4's 0 is "unknown"; numbers that aren't are ignored
    odd = tags_from({"trkn": [(3, 0)], "discnumber": ["A"], "tracktotal": ["x"]})
    assert (odd.track_number, odd.track_total, odd.disc_number) == (3, None, None)


@pytest.mark.parametrize(
    ("numbers", "tracks", "complete"),
    [
        ([(1, 2, None, None), (2, 2, None, None)], 2, True),
        ([(1, 2, None, None)], 2, False),
        ([(1, 2, 1, 2), (2, 2, 1, 2), (1, 3, 2, 2)], 5, False),  # most of disc 2 missing
        ([(1, 2, 1, 2), (2, 2, 1, 2)], None, False),  # disc 2 not there: its count unknown
        ([(1, 1, None, None), (2, 1, None, None)], 1, True),  # an extra track
        ([(1, None, None, None)], None, False),
    ],
)
def test_coverage(numbers, tracks, complete):
    files = [
        LibraryFile(f"{n}", 1, 1, track_number=t, track_total=of, disc_number=d, disc_total=dt)
        for n, (t, of, d, dt) in enumerate(numbers)
    ]
    assert library.coverage(files) == (tracks, complete)


def test_track_tags_read_again_after_their_migration(tmp_path: Path):
    root = tmp_path / "music"
    album(root, "A", REL, tracks=1)
    path = tmp_path / "db.sqlite"
    old = sqlite3.connect(path)  # a database from before 16 (all SQL up to 14), file scanned
    for index, script in enumerate(MIGRATIONS[:14], start=1):
        old.executescript(script)
        old.execute(f"PRAGMA user_version = {index}")
    stat = (root / "A/01.flac").stat()
    old.execute(
        "INSERT INTO library_files (user_id, path, mtime_ns, size, release_mbid) "
        "VALUES (1, 'A/01.flac', ?, ?, ?)",
        (stat.st_mtime_ns, stat.st_size, REL),
    )
    old.commit()
    old.close()
    with Store(path) as store:
        assert scan(store, OWNER_ID, root, now=NOW).read == 1
        (file,) = store.library_files(OWNER_ID)
        (release,) = store.library(OWNER_ID)
    assert (file.track_number, file.track_total) == (1, 1)
    assert (release.tracks, release.complete) == (1, True)


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
    assert "Nothing in your library yet" in client.get("/library").text
    page = client.get("/library/scan").text
    assert "No music folder yet" in page and "Rescan" not in page
    assert client.post("/library/scan").status_code == 404


def test_library_page_and_rescan(tmp_path: Path):
    root = tmp_path / "music"
    album(root, "Artist/Album", REL)
    flac(root / "Loose/untagged.flac")
    settings = settings_for(tmp_path, library_path=root)
    app = create_app(settings)
    client = TestClient(app)
    assert "Not scanned yet" in client.get("/library/scan").text

    status = client.post("/library/scan", headers={"HX-Request": "true"}).text
    assert 'hx-trigger="every 3s"' in status and "Scanning" in status
    assert app.state.scanner.scanning

    app.state.scanner.run_once(full=True)  # what the background thread would do
    app.state.scanner._asked = False
    page = client.get("/library/scan").text
    assert 'action="/library/scan"' in page and "Rescan" in page
    assert "1 release (1 album) from 3 files" in " ".join(page.split())
    assert "Last scanned" in page and "every 3s" not in page
    assert "<code>Loose</code>" in page

    response = client.post("/library/scan", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/library/scan"


# --- "In your library" (5b) ---------------------------------------------------------------

RG_BRAT = "0a1b2c3d-0000-4000-8000-0000000000b1"
R_CD = "0a1b2c3d-0000-4000-8000-0000000000c1"
R_VINYL = "0a1b2c3d-0000-4000-8000-0000000000c2"
R_NO_GROUP = "0a1b2c3d-0000-4000-8000-0000000000c3"
FOUND_AT = "2026-10-30T09:00:00+00:00"


def release(mbid: str, release_group: str | None, title: str) -> LibraryRelease:
    return LibraryRelease(mbid, release_group, (), title, "Charli xcx", title, 2)


def owning(store: Store, *releases: LibraryRelease) -> None:
    store.replace_library(OWNER_ID, releases, FOUND_AT)


def test_in_library_by_release_group_or_release(tmp_path: Path):
    with Store(tmp_path / "db.sqlite") as store:
        owning(
            store,
            release(R_CD, RG_BRAT, "Brat"),
            release(R_NO_GROUP, None, "Untagged group"),
        )
        found = store.in_library(OWNER_ID, [RG_BRAT, R_NO_GROUP, R_VINYL])
        assert found == {RG_BRAT: FOUND_AT, R_NO_GROUP: FOUND_AT}
        assert store.in_library(OWNER_ID + 1, [RG_BRAT]) == {}  # only your own library
        assert store.in_library(OWNER_ID, []) == {}


def test_wishlist_items_found_in_the_library(tmp_path: Path):
    with Store(tmp_path / "db.sqlite") as store:
        owning(store, release(R_CD, RG_BRAT, "Brat"))
        items = [
            WishlistItem(RG_BRAT, "Brat", "Charli xcx", id=1),  # any edition
            WishlistItem(RG_BRAT, "Brat", "Charli xcx", release_mbid=R_CD, id=2),
            WishlistItem(RG_BRAT, "Brat", "Charli xcx", release_mbid=R_VINYL, id=3),
            WishlistItem(R_NO_GROUP, "Other", "Someone", id=4),
        ]
        assert library.found(store, OWNER_ID, items) == {
            1: library.Found(FOUND_AT, other_edition=False),
            2: library.Found(FOUND_AT, other_edition=False),
            3: library.Found(FOUND_AT, other_edition=True),
        }


def test_owned_last_keeps_the_order_otherwise():
    assert library.owned_last("abcd", {"b", "d"}, str) == ["a", "c", "b", "d"]


def test_wishlist_offers_found_items_as_bought(tmp_path: Path):
    client = TestClient(create_app(settings_for(tmp_path)), follow_redirects=False)
    client.post(
        "/wishlist", data={"release_group": RG_BRAT, "title": "Brat", "artist_credit": "Charli"}
    )
    assert "In your library" not in client.get("/wishlist").text
    with Store(tmp_path / "groovehound.db") as store:
        owning(store, release(R_CD, RG_BRAT, "Brat"))
        (item,) = store.wishlist(OWNER_ID)
    page = client.get("/wishlist").text
    assert "In your library" in page and "Mark bought" in page and "Bought it" not in page
    assert "In your library" in client.get("/").text
    with Store(tmp_path / "groovehound.db") as store:
        assert store.wishlist_item(OWNER_ID, item.id).bought_at is None  # offered, not done

    client.post(f"/wishlist/{item.id}/bought", data={"bought": "1"})
    bought = client.get("/wishlist", params={"show": "bought"}).text
    assert "In your library" in bought and "Mark bought" not in bought
