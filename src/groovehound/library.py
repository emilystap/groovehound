"""Library scan (roadmap 5a): what's in your music folder, from the MusicBrainz tags Picard
writes. No requests to anyone: the folder is local (mounted read-only in Docker), and files are
only read.

Each audio file's tags give its release (*MusicBrainz Album Id*), release group, album artists
(*MusicBrainz Album Artist Id*, else *Artist Id*), album title and album artist name, read with
`mutagen` from Vorbis comments (FLAC, Ogg, Opus), ID3 `TXXX` frames (MP3, AIFF, WAV), MP4
freeform atoms, APEv2 and ASF, which all name them alike. A file without an album id is
counted and listed as untagged, never guessed at by name. The library is one row per release,
rebuilt from the per-file index after every scan; the index keeps each file's mtime and size,
so a rescan only re-reads files that changed.

Scans run when you ask (*Rescan* on the Library page, `groovehound scan`) and on a slow
background trickle (`LibraryScanner`): it wakes at a random interval, never at a fixed time of
day, walks the folder (a `stat` per file) and reads at most `TRICKLE_READS` changed files, so a
big library isn't read all at once. A folder that's missing, or has no audio files where the
index had some (a share that isn't mounted), stops the scan without changing anything.
"""

from __future__ import annotations

import logging
import os
import random
import re
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import mutagen

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
}


@dataclass(frozen=True)
class FileTags:
    """What a file's tags say about the release it's from."""

    release_mbid: str | None = None
    release_group_mbid: str | None = None
    album_artist_mbids: tuple[str, ...] = ()
    album: str = ""
    album_artist: str = ""


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
    return FileTags(
        release_mbid=release[0] if release else None,
        release_group_mbid=release_group[0] if release_group else None,
        album_artist_mbids=artists,
        album=(found.get("album") or [""])[0],
        album_artist=names[0],
    )


def read_tags(path: Path) -> FileTags:
    """A file's MusicBrainz tags. Raises OSError or mutagen.MutagenError if it can't be read,
    and ValueError if it isn't audio mutagen knows."""
    audio = mutagen.File(path)
    if audio is None:
        raise ValueError("not an audio format Groovehound can read")
    return tags_from(audio.tags)


def _normalized(key: str) -> str:
    key = key.lower()
    for prefix in _KEY_PREFIXES:
        key = key.removeprefix(prefix)
    return re.sub(r"[^a-z0-9]", "", key)


def _texts(value: Any) -> list[str]:
    """A tag value's strings: ID3 frames (.text), lists (Vorbis, MP4, ASF), APEv2 values;
    MP4 freeform values are bytes. Multiple values in one string are split on NUL."""
    items = value.text if hasattr(value, "text") else value
    if isinstance(items, (str, bytes)) or not isinstance(items, (list, tuple)):
        items = [items]
    texts = []
    for item in items:
        text = item.decode("utf-8", "replace") if isinstance(item, bytes) else str(item)
        texts += (t.strip() for t in text.split("\0"))
    return [t for t in texts if t]


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
    )


def library_releases(files: Iterable[LibraryFile]) -> list[LibraryRelease]:
    """One release per album id among the tagged files: its release group, album artists, title
    and album artist as most of its files have them, and the folder holding all of them."""
    by_release: dict[str, list[LibraryFile]] = {}
    for f in files:
        if f.release_mbid:
            by_release.setdefault(f.release_mbid, []).append(f)
    releases = []
    for mbid, tracks in by_release.items():
        folders = [PurePosixPath(t.path).parent for t in tracks]
        releases.append(
            LibraryRelease(
                release_mbid=mbid,
                release_group_mbid=_most(t.release_group_mbid for t in tracks),
                album_artist_mbids=_most(t.album_artist_mbids for t in tracks) or (),
                title=_most(t.album for t in tracks) or "",
                artist=_most(t.album_artist for t in tracks) or "",
                folder=os.path.commonpath([str(f) for f in folders]).replace("\\", "/"),
                files=len(tracks),
            )
        )
    return releases


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


def untagged_folders(files: Iterable[LibraryFile]) -> list[tuple[str, int]]:
    """Folders with files that have no album id, and how many, by folder."""
    counts: dict[str, int] = {}
    for f in files:
        if not f.release_mbid and not f.problem:
            folder = str(PurePosixPath(f.path).parent)
            counts[folder] = counts.get(folder, 0) + 1
    return sorted(counts.items(), key=lambda c: c[0].casefold())


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
