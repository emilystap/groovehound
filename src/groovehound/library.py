"""Library scan (roadmap 5a): what's in your music folder, from the MusicBrainz tags Picard
writes. No requests to anyone: the folder is local (mounted read-only in Docker), and files are
only read.

Each audio file's tags give its release (*MusicBrainz Album Id*), release group, album artists
(*MusicBrainz Album Artist Id*, else *Artist Id*), album title and album artist name, read with
`mutagen` from Vorbis comments (FLAC, Ogg, Opus), ID3 `TXXX` frames (MP3, AIFF, WAV), MP4
freeform atoms, APEv2 and ASF, which all name them alike; also its recording and release track
(roadmap 5k), so a song owned anywhere counts wherever it appears, and its title, track artist,
track and disc numbers with their totals, length, medium format and disc title, so an edition
you have every track of has its track list without asking MusicBrainz (`coverage`,
`tracks.local_track_list`). A file without an album id is counted and listed as untagged, never
guessed at by name. The library is one row per release,
rebuilt from the per-file index after every scan; the index keeps each file's mtime and size,
so a rescan only re-reads files that changed.

Scans run when you ask (*Rescan* on the Library page, `groovehound scan`) and on a slow
background trickle (`LibraryScanner`): it wakes at a random interval, never at a fixed time of
day, walks the folder (a `stat` per file) and reads at most `TRICKLE_READS` changed files, so a
big library isn't read all at once. A folder that's missing, or has no audio files where the
index had some (a share that isn't mounted), stops the scan without changing anything.
"""

from __future__ import annotations

import base64
import logging
import os
import random
import re
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import mutagen
from mutagen.flac import Picture

from .config import Settings
from .db import OWNER_ID, LibraryFile, LibraryRelease, Store, WishlistItem

log = logging.getLogger(__name__)

AUDIO_SUFFIXES = frozenset(
    {
        ".flac", ".ogg", ".oga", ".opus", ".spx", ".mp3", ".m4a", ".m4b", ".mp4", ".alac",
        ".aif", ".aiff", ".wav", ".wv", ".ape", ".mpc", ".wma", ".asf", ".dsf", ".tta",
    }
)  # fmt: skip
WAKE_INTERVAL = (3600.0, 3 * 3600.0)  # seconds asleep between trickle scans (1-3 h), drawn afresh
TRICKLE_READS = 2000  # changed files read per trickle scan; a scan you ask for reads them all
SAVE_EVERY = 200  # files read between saves (and progress updates on the Library page)

_MBID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_KEY_PREFIXES = ("txxx:", "----:com.apple.itunes:")
# Tag names, lowercased with prefixes and anything but letters and digits removed, so that
# Vorbis MUSICBRAINZ_ALBUMID, ID3 TXXX:MusicBrainz Album Id, MP4 ----:com.apple.iTunes:MusicBrainz
# Album Id, APEv2 MUSICBRAINZ_ALBUMID and ASF MusicBrainz/Album Id all read the same.
_FIELDS = {
    "musicbrainzalbumid": "release",
    "musicbrainzreleasegroupid": "release_group",
    "musicbrainzalbumartistid": "album_artists",
    "musicbrainzartistid": "artists",
    "musicbrainztrackid": "recording",  # the recording, despite the name (roadmap 5k)
    "ufidhttpmusicbrainzorg": "recording",  # ID3 keeps it in a UFID frame, not TXXX
    "musicbrainzreleasetrackid": "track_id",  # the track on this release
    "album": "album",
    "talb": "album",
    "alb": "album",  # MP4 ©alb
    "wmalbumtitle": "album",
    "albumartist": "album_artist",
    "tpe2": "album_artist",
    "aart": "album_artist",
    "wmalbumartist": "album_artist",
    "artist": "artist",
    "tpe1": "artist",
    "art": "artist",  # MP4 ©ART
    "author": "artist",  # ASF
    "title": "title",
    "tit2": "title",
    "nam": "title",  # MP4 ©nam
    "tracknumber": "track",  # "3", or "3/12"
    "trck": "track",
    "trkn": "track",  # MP4: (3, 12)
    "track": "track",  # APEv2
    "wmtracknumber": "track",
    "tracktotal": "track_total",
    "totaltracks": "track_total",
    "discnumber": "disc",
    "tpos": "disc",
    "disk": "disc",  # MP4: (1, 2)
    "disc": "disc",  # APEv2
    "wmpartofset": "disc",
    "disctotal": "disc_total",
    "totaldiscs": "disc_total",
    "media": "media",
    "tmed": "media",
    "wmmedia": "media",
    "discsubtitle": "disc_title",
    "tsst": "disc_title",
    "wmsetsubtitle": "disc_title",
}


@dataclass(frozen=True)
class FileTags:
    """What a file's tags say about the release it's from."""

    release_mbid: str | None = None
    release_group_mbid: str | None = None
    album_artist_mbids: tuple[str, ...] = ()
    album: str = ""
    album_artist: str = ""
    recording_mbid: str | None = None
    track_mbid: str | None = None
    title: str = ""
    artist: str = ""  # the track's
    track_number: int | None = None
    track_total: int | None = None
    disc_number: int | None = None
    disc_total: int | None = None
    length_ms: int | None = None  # from the audio, not a tag (`read_tags`)
    media: str = ""
    disc_title: str = ""


def tags_from(tags: Mapping[str, Any] | None) -> FileTags:
    """The MusicBrainz ids and names in a mutagen tags mapping (any format)."""
    found: dict[str, list[str]] = {}
    for key, value in (tags or {}).items():
        name = _FIELDS.get(_normalized(str(key)))
        if name:
            found.setdefault(name, []).extend(_texts(value))
    release = _mbids(found.get("release", ()))
    release_group = _mbids(found.get("release_group", ()))
    artists = _mbids(found.get("album_artists", ())) or _mbids(found.get("artists", ()))
    names = found.get("album_artist") or found.get("artist") or [""]
    recording = _mbids(found.get("recording", ()))
    track = _mbids(found.get("track_id", ()))
    track_number, track_total = _pair(found.get("track"))
    disc_number, disc_total = _pair(found.get("disc"))
    return FileTags(
        release_mbid=release[0] if release else None,
        release_group_mbid=release_group[0] if release_group else None,
        album_artist_mbids=artists,
        album=(found.get("album") or [""])[0],
        album_artist=names[0],
        recording_mbid=recording[0] if recording else None,
        track_mbid=track[0] if track else None,
        title=(found.get("title") or [""])[0],
        artist=(found.get("artist") or [""])[0],
        track_number=track_number,
        track_total=track_total or _number(found.get("track_total")),
        disc_number=disc_number,
        disc_total=disc_total or _number(found.get("disc_total")),
        media=(found.get("media") or [""])[0],
        disc_title=(found.get("disc_title") or [""])[0],
    )


def read_tags(path: Path) -> FileTags:
    """A file's MusicBrainz tags, and its length. Raises OSError or mutagen.MutagenError if it
    can't be read, and ValueError if it isn't audio mutagen knows."""
    audio = mutagen.File(path)
    if audio is None:
        raise ValueError("not an audio format Groovehound can read")
    tags = tags_from(audio.tags)
    length = round((getattr(audio.info, "length", 0) or 0) * 1000)
    return replace(tags, length_ms=length) if length > 0 else tags


def _normalized(key: str) -> str:
    key = key.lower()
    for prefix in _KEY_PREFIXES:
        key = key.removeprefix(prefix)
    return re.sub(r"[^a-z0-9]", "", key)


def _texts(value: Any) -> list[str]:
    """A tag value's strings: ID3 frames (.text), lists (Vorbis, MP4, ASF), APEv2 values;
    MP4 freeform values are bytes, as is an ID3 UFID frame's data; MP4 track and disc numbers
    are (number, total) pairs, read as "3/12". Multiple values in one string are split on
    NUL."""
    if hasattr(value, "text"):
        items = value.text
    elif isinstance(getattr(value, "data", None), bytes):  # ID3 UFID
        items = value.data
    else:
        items = value
    if isinstance(items, (str, bytes)) or not isinstance(items, (list, tuple)):
        items = [items]
    texts = []
    for item in items:
        if isinstance(item, tuple):
            text = "/".join(str(i) for i in item)
        elif isinstance(item, bytes):
            text = item.decode("utf-8", "replace")
        else:
            text = str(item)
        texts += (t.strip() for t in text.split("\0"))
    return [t for t in texts if t]


def _pair(texts: list[str] | None) -> tuple[int | None, int | None]:
    """A track or disc number and the total, from "3/12" or "3" (the total None)."""
    number, _, total = (texts or [""])[0].partition("/")
    return _number([number]), _number([total])


def _number(texts: list[str] | None) -> int | None:
    """The first string as a positive whole number, else None (0 is how MP4 says unknown)."""
    try:
        number = int((texts or [""])[0].strip())
    except ValueError:
        return None
    return number if number > 0 else None


def _mbids(texts: Iterable[str]) -> tuple[str, ...]:
    """Every MBID in these strings, in order, once each (ID3v2.3 joins several with "/")."""
    return tuple(dict.fromkeys(m for t in texts for m in _MBID.findall(t.lower())))


# --- Scanning ----------------------------------------------------------------------------


@dataclass(frozen=True)
class ScanResult:
    files: int = 0  # audio files found
    read: int = 0  # files whose tags were (re)read
    removed: int = 0  # files gone since the last scan
    left: int = 0  # changed files not read yet (a trickle scan reads at most TRICKLE_READS)
    releases: int = 0
    error: str = ""  # why the scan stopped without changing anything


@dataclass
class _Walk:
    files: dict[str, tuple[int, int]] = field(default_factory=dict)  # path -> (mtime_ns, size)
    skipped: list[str] = field(default_factory=list)  # folders that couldn't be read


def walk(root: Path) -> _Walk:
    """Every audio file under `root` (relative "/"-separated path -> mtime and size). Hidden
    files and folders are left out; symlinked folders aren't followed (no loops)."""
    found = _Walk()
    stack = [""]
    while stack:
        folder = stack.pop()
        try:
            with os.scandir(root / folder if folder else root) as entries:
                for entry in entries:
                    if entry.name.startswith("."):
                        continue
                    path = f"{folder}/{entry.name}" if folder else entry.name
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(path)
                    elif Path(entry.name).suffix.lower() in AUDIO_SUFFIXES and entry.is_file():
                        stat = entry.stat()
                        found.files[path] = (stat.st_mtime_ns, stat.st_size)
        except OSError as exc:
            if not folder:
                raise
            log.warning("Library folder %s not read: %s", folder, exc)
            found.skipped.append(folder)
    return found


def scan(
    store: Store,
    user_id: int,
    root: Path,
    *,
    limit: int | None = None,
    now: datetime | None = None,
    read: Callable[[Path], FileTags] = read_tags,
) -> ScanResult:
    """Scan the library folder `root` for this user: re-read new and changed files (at most
    `limit`), forget files that are gone, and rebuild the library from the index. The scan's
    progress and outcome are saved for the Library page."""
    stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")
    store.start_library_scan(user_id, str(root), stamp)
    result = _scan(store, user_id, root, limit, stamp, read)
    store.finish_library_scan(user_id, result.files, result.read, result.left, result.error)
    return result


def _scan(
    store: Store,
    user_id: int,
    root: Path,
    limit: int | None,
    stamp: str,
    read: Callable[[Path], FileTags],
) -> ScanResult:
    if not root.is_dir():
        return ScanResult(error=f"the library folder {root} isn't there (not mounted?)")
    try:
        found = walk(root)
    except OSError as exc:
        return ScanResult(error=f"the library folder {root} can't be read: {exc}")
    index = store.library_index(user_id)
    if not found.files and index:
        return ScanResult(
            error=f"no audio files in {root}, where there were {len(index)} (not mounted?)"
        )

    skipped = tuple(f"{folder}/" for folder in found.skipped)
    gone = [p for p in index if p not in found.files and not p.startswith(skipped)]
    store.forget_library_files(user_id, gone)
    changed = sorted(p for p, stat in found.files.items() if index.get(p) != stat)
    todo = changed if limit is None else changed[:limit]

    batch: list[LibraryFile] = []
    for count, path in enumerate(todo, start=1):
        batch.append(_file(root, path, found.files[path], read))
        if len(batch) >= SAVE_EVERY:
            store.save_library_files(user_id, batch)
            store.library_scan_progress(user_id, len(found.files), count)
            batch = []
    store.save_library_files(user_id, batch)

    releases = library_releases(store.library_files(user_id))
    store.replace_library(user_id, releases, stamp)
    return ScanResult(
        files=len(found.files),
        read=len(todo),
        removed=len(gone),
        left=len(changed) - len(todo),
        releases=len(releases),
    )


def _file(
    root: Path, path: str, stat: tuple[int, int], read: Callable[[Path], FileTags]
) -> LibraryFile:
    mtime_ns, size = stat
    try:
        tags = read(root / path)
    except (OSError, ValueError, mutagen.MutagenError) as exc:
        return LibraryFile(path, mtime_ns, size, problem=str(exc) or type(exc).__name__)
    return LibraryFile(
        path,
        mtime_ns,
        size,
        release_mbid=tags.release_mbid,
        release_group_mbid=tags.release_group_mbid,
        album_artist_mbids=tags.album_artist_mbids,
        album=tags.album,
        album_artist=tags.album_artist,
        recording_mbid=tags.recording_mbid,
        track_mbid=tags.track_mbid,
        title=tags.title,
        artist=tags.artist,
        track_number=tags.track_number,
        track_total=tags.track_total,
        disc_number=tags.disc_number,
        disc_total=tags.disc_total,
        length_ms=tags.length_ms,
        media=tags.media,
        disc_title=tags.disc_title,
    )


def library_releases(files: Iterable[LibraryFile]) -> list[LibraryRelease]:
    """One release per album id among the tagged files: its release group, album artists, title
    and album artist as most of its files have them, the folder holding all of them, and its
    track count and whether you have every track, as their tags say (`coverage`)."""
    by_release: dict[str, list[LibraryFile]] = {}
    for f in files:
        if f.release_mbid:
            by_release.setdefault(f.release_mbid, []).append(f)
    releases = []
    for mbid, tracks in by_release.items():
        folders = [PurePosixPath(t.path).parent for t in tracks]
        total, complete = coverage(tracks)
        releases.append(
            LibraryRelease(
                release_mbid=mbid,
                release_group_mbid=_most(t.release_group_mbid for t in tracks),
                album_artist_mbids=_most(t.album_artist_mbids for t in tracks) or (),
                title=_most(t.album for t in tracks) or "",
                artist=_most(t.album_artist for t in tracks) or "",
                folder=os.path.commonpath([str(f) for f in folders]).replace("\\", "/"),
                files=len(tracks),
                tracks=total,
                complete=complete,
            )
        )
    return releases


def disc_totals(files: Iterable[LibraryFile]) -> dict[int, int] | None:
    """Each disc of an edition and its track count, as its files' tags say (as most of a disc's
    files say it, and one disc if none says how many); None unless they say it for every disc."""
    files = list(files)
    discs = _most(f.disc_total for f in files)
    if discs is None:
        if any((f.disc_number or 1) != 1 for f in files):
            return None  # several discs, but not how many
        discs = 1
    totals = {}
    for disc in range(1, discs + 1):
        total = _most(f.track_total for f in files if (f.disc_number or 1) == disc)
        if total is None:
            return None
        totals[disc] = total
    return totals


def coverage(files: Iterable[LibraryFile]) -> tuple[int | None, bool]:
    """An edition's track count as its files' tags give it (None if they don't for every disc),
    and whether there's a file for every one of those tracks (the same track in two formats
    counts once, and tracks beyond the count are extras)."""
    files = list(files)
    totals = disc_totals(files)
    if totals is None:
        return None, False
    have = {(f.disc_number or 1, f.track_number) for f in files if f.track_number}
    want = {(disc, n) for disc, total in totals.items() for n in range(1, total + 1)}
    return sum(totals.values()), want <= have


def _most[T](values: Iterable[T]) -> T | None:
    """The commonest value that isn't empty (the first of equals), or None."""
    counts: dict[T, int] = {}
    for value in values:
        if value:
            counts[value] = counts.get(value, 0) + 1
    return max(counts, key=counts.__getitem__) if counts else None


@dataclass(frozen=True)
class Found:
    """A wishlist item found in the library (roadmap 5b)."""

    since: str  # when the library scan first found it
    other_edition: bool  # the item is for one edition, and the library has another


def found(store: Store, user_id: int, items: Iterable[WishlistItem]) -> dict[int, Found]:
    """Which of these wishlist items are in the user's library, by item id: the edition it's
    for, or with no edition chosen, any edition of its release group. Only ever offered as
    bought, never marked so."""
    items = list(items)
    owned = store.in_library(
        user_id, (m for i in items for m in (i.release_group_mbid, i.release_mbid) if m)
    )
    result = {}
    for item in items:
        if item.id is None:
            continue
        if item.release_mbid in owned:
            result[item.id] = Found(owned[item.release_mbid], other_edition=False)
        elif item.release_group_mbid in owned:
            result[item.id] = Found(owned[item.release_group_mbid], bool(item.release_mbid))
    return result


def owned_last[T](things: Iterable[T], owned: Iterable[str], mbid: Callable[[T], str]) -> list[T]:
    """`things` in the same order, but those whose release group is in `owned` after the rest."""
    owned = set(owned)
    things = list(things)
    return [t for t in things if mbid(t) not in owned] + [t for t in things if mbid(t) in owned]


@dataclass(frozen=True)
class Problems:
    """What a scan couldn't use, for the Scan page."""

    untagged: list[tuple[str, int]]  # folders, with how many files
    no_release_group: list[LibraryRelease]
    unreadable: list[LibraryFile]

    @property
    def count(self) -> int:
        return len(self.untagged) + len(self.no_release_group) + len(self.unreadable)


def problems(store: Store, user_id: int) -> Problems:
    """What the latest scan couldn't use (no requests)."""
    untagged = store.library_files(user_id, tagged=False)
    return Problems(
        untagged_folders(untagged),
        [r for r in store.library(user_id) if not r.release_group_mbid],
        [f for f in untagged if f.problem],
    )


def untagged_folders(files: Iterable[LibraryFile]) -> list[tuple[str, int]]:
    """Folders with files that have no album id, and how many, by folder."""
    counts: dict[str, int] = {}
    for f in files:
        if not f.release_mbid and not f.problem:
            folder = str(PurePosixPath(f.path).parent)
            counts[folder] = counts.get(folder, 0) + 1
    return sorted(counts.items(), key=lambda c: c[0].casefold())


# --- Cover art (roadmap 5i) -------------------------------------------------------------

# Image files beside the music that hold the front cover, best first (matched ignoring case)
COVER_NAMES = ("cover", "folder", "front", "albumart", "album")
COVER_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif")
MAX_ART_BYTES = 30_000_000  # an image file bigger than this isn't read
FRONT_COVER = 3  # the picture type ID3 APIC and FLAC pictures give the front cover


@dataclass(frozen=True)
class Art:
    """Cover art found in the library: the image as stored, and the file it came from."""

    image: bytes
    source: str  # relative to the library folder, "/"-separated
    mtime_ns: int


def cover_art(store: Store, user_id: int, root: Path, release_group_mbid: str) -> Iterator[Art]:
    """Cover art for a release group in the user's library, best first: an image file in the
    folder of one of its editions (`cover.jpg`, `folder.jpg`, `front.png` and the like), then
    the art embedded in their files (the front cover if one is marked). Lazy, so a caller
    that takes the first one reads no more than it needs. Nothing for a release group that
    isn't in the library."""
    editions = [
        (e.folder, store.library_paths(user_id, e.release_mbid))
        for e in store.library_editions(user_id, release_group_mbid)
    ]
    seen: set[str] = set()
    for folder, paths in editions:
        # the edition's folder, then those of its files (a disc's subfolder)
        for where in dict.fromkeys([folder, *(str(PurePosixPath(p).parent) for p in paths)]):
            if where not in seen:
                seen.add(where)
                yield from _art_files(root, "" if where == "." else where)
    for _, paths in editions:
        for path in paths[:2]:  # an album's tracks usually all carry the same art
            art = _embedded(root, path)
            if art is not None:
                yield art


def _art_files(root: Path, folder: str) -> Iterator[Art]:
    try:
        with os.scandir(root / folder if folder else root) as entries:
            names = {e.name.lower(): e.name for e in entries if e.is_file()}
    except OSError:
        return
    for stem in COVER_NAMES:
        for suffix in COVER_SUFFIXES:
            name = names.get(stem + suffix)
            if name is None:
                continue
            path = f"{folder}/{name}" if folder else name
            try:
                stat = (root / path).stat()
                if stat.st_size > MAX_ART_BYTES:
                    continue
                image = (root / path).read_bytes()
            except OSError:
                continue
            if image:
                yield Art(image, path, stat.st_mtime_ns)


def _embedded(root: Path, path: str) -> Art | None:
    """The art embedded in an audio file, if any: the front cover, else the first picture."""
    try:
        mtime_ns = (root / path).stat().st_mtime_ns
        audio = mutagen.File(root / path)
    except (OSError, mutagen.MutagenError):
        return None
    if audio is None:
        return None
    pictures = sorted(_pictures(audio), key=lambda p: not p[0])  # front covers first
    return Art(pictures[0][1], path, mtime_ns) if pictures else None


def _pictures(audio: Any) -> Iterator[tuple[bool, bytes]]:
    """(is the front cover, image) for each picture in a mutagen file: FLAC pictures, Vorbis
    METADATA_BLOCK_PICTURE (Ogg, Opus), ID3 APIC (MP3, AIFF, WAV), MP4 covr (unmarked) and
    APEv2 *Cover Art (Front)*."""
    for picture in getattr(audio, "pictures", None) or ():
        yield picture.type == FRONT_COVER, picture.data
    tags = audio.tags
    if not tags:
        return
    if hasattr(tags, "getall"):  # ID3
        for frame in tags.getall("APIC"):
            yield frame.type == FRONT_COVER, frame.data
        return
    for key, values in tags.items():
        key = str(key).lower()
        if key == "metadata_block_picture":
            for value in values:
                try:
                    picture = Picture(base64.b64decode(value))
                except (ValueError, mutagen.MutagenError):
                    continue
                yield picture.type == FRONT_COVER, picture.data
        elif key == "covr":
            for value in values:
                yield False, bytes(value)
        elif key == "cover art (front)":
            # APEv2 binary: the file name, NUL, the image
            yield True, bytes(getattr(values, "value", b"")).partition(b"\0")[2]


# --- The background trickle ----------------------------------------------------------------


class LibraryScanner:
    """Scans the owner's library folder (`library_path`, until per-user settings exist): a
    small scan at random intervals, a full one on `request()`. `start()` with the app,
    `shutdown()` when it stops."""

    def __init__(
        self,
        settings: Settings,
        *,
        interval: tuple[float, float] = WAKE_INTERVAL,
        batch: int = TRICKLE_READS,
    ) -> None:
        if settings.library_path is None:
            raise ValueError("no library folder configured")
        self.settings = settings
        self.root: Path = settings.library_path
        self.interval = interval
        self.batch = batch
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._asked = False  # a full scan was requested and hasn't finished
        self._thread: threading.Thread | None = None

    @property
    def scanning(self) -> bool:
        return self._asked

    def request(self) -> None:
        """Scan everything now (in the background), unless that's already under way."""
        self._asked = True
        self._wake.set()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="groovehound-library", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def run_once(self, *, full: bool = False) -> ScanResult:
        with Store(self.settings.database_path) as store:
            result = scan(store, OWNER_ID, self.root, limit=None if full else self.batch)
        if result.error:
            log.warning("Library scan stopped: %s", result.error)
        elif result.read or result.removed:
            log.info(
                "Scanned the library: %d file(s) read, %d gone, %d release(s)%s",
                result.read,
                result.removed,
                result.releases,
                f", {result.left} file(s) left for later" if result.left else "",
            )
        return result

    def _loop(self) -> None:
        while True:
            self._wake.wait(random.uniform(*self.interval))
            if self._stop.is_set():
                return
            self._wake.clear()
            full = self._asked
            try:
                self.run_once(full=full)
            except Exception:  # keep the trickle alive whatever happens
                log.exception("Library scan failed")
            finally:
                if full:  # a click during a full scan doesn't start another
                    self._wake.clear()
                    self._asked = False
