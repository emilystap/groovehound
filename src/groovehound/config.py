"""Settings: defaults < TOML config file < environment variables < explicit overrides.

Environment variables (handy in Docker compose):
    GROOVEHOUND_CONFIG                path to config.toml
    GROOVEHOUND_DB                    SQLite database path
    GROOVEHOUND_CONTACT               your email (or URL), sent in the User-Agent to MusicBrainz
                                      and ListenBrainz; required for anything that calls them
    GROOVEHOUND_MUSICBRAINZ_API_ROOT  another MusicBrainz server (your own mirror)
    GROOVEHOUND_MUSICBRAINZ_INTERVAL  seconds between MusicBrainz requests; mirrors only
    GROOVEHOUND_LISTENBRAINZ_USER     your ListenBrainz username (for personal recommendations)
    GROOVEHOUND_LISTENBRAINZ_TOKEN    your ListenBrainz user token (optional)
    GROOVEHOUND_QOBUZ_STORE           Qobuz store for search links, e.g. gb-en, fr-fr
    GROOVEHOUND_LIBRARY               your music folder (read-only), to know what you have
    GROOVEHOUND_FOLLOW_TYPES          release group types a new follow is for, comma-separated
                                      (e.g. album,ep,single)
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATHS = (Path("config.toml"), Path("/config/config.toml"))


@dataclass(frozen=True)
class Settings:
    database_path: Path = Path("data/groovehound.db")
    # How MusicBrainz and ListenBrainz can reach whoever runs this instance (an email address or
    # URL). Both ask for one in the User-Agent; without it nothing calls either service.
    contact: str = ""
    # Another MusicBrainz server (your own mirror, e.g. musicbrainz-docker): its web service
    # root, "http://host:5000" or ".../ws/2". Empty = musicbrainz.org.
    musicbrainz_api_root: str = ""
    # Seconds from each response to the next request; None = 1.5. Never below 1.5 for
    # musicbrainz.org (musicbrainz.client_for refuses it); a mirror of your own can use 0.
    musicbrainz_interval: float | None = None
    # ListenBrainz account for personal fresh releases and recommendations (user 1's, until
    # these move to the users table). The token is optional and never logged.
    listenbrainz_user: str = ""
    listenbrainz_token: str = field(default="", repr=False)
    # Which Qobuz store the wishlist's search links open: its country-language prefix, as in
    # qobuz.com/gb-en/ (buy.QOBUZ_STORE checks it).
    qobuz_store: str = "us-en"
    # Your music folder, read (never written) for the MusicBrainz tags Picard writes, to know
    # what you already have (library.py); None = no library. In Docker, mount it read-only.
    library_path: Path | None = None
    # The release group types a new follow is for (follows.TYPES; follows.clean_types checks
    # them at startup). Each follow can be changed afterwards on the artist pages.
    follow_types: tuple[str, ...] = ("album", "ep")


def _types(value: str) -> tuple[str, ...]:
    return tuple(t.strip().lower() for t in value.split(",") if t.strip())


def _from_toml(data: Mapping[str, Any], base: Settings) -> Settings:
    database = data.get("database", {})
    brainz = data.get("brainz", {})
    musicbrainz = data.get("musicbrainz", {})
    listenbrainz = data.get("listenbrainz", {})
    stores = data.get("stores", {})
    library = data.get("library", {})
    follows = data.get("follows", {})
    return replace(
        base,
        database_path=Path(database.get("path", base.database_path)),
        contact=str(brainz.get("contact", base.contact)),
        musicbrainz_api_root=str(musicbrainz.get("api_root", base.musicbrainz_api_root)),
        musicbrainz_interval=(
            float(musicbrainz["interval"])
            if "interval" in musicbrainz
            else base.musicbrainz_interval
        ),
        listenbrainz_user=str(listenbrainz.get("user", base.listenbrainz_user)),
        listenbrainz_token=str(listenbrainz.get("token", base.listenbrainz_token)),
        qobuz_store=str(stores.get("qobuz", base.qobuz_store)),
        library_path=Path(library["path"]) if library.get("path") else base.library_path,
        follow_types=(
            tuple(str(t).strip().lower() for t in follows["types"])
            if "types" in follows
            else base.follow_types
        ),
    )


def load_settings(
    config_path: Path | None = None,
    env: Mapping[str, str] | None = None,
    **overrides: Any,
) -> Settings:
    """Build Settings from (in increasing priority) defaults, a TOML file, env vars, overrides.

    `config_path` (or GROOVEHOUND_CONFIG) must exist if given; otherwise ./config.toml and
    /config/config.toml are tried. Overrides with value None are ignored.
    """
    env = os.environ if env is None else env
    settings = Settings()

    explicit = config_path or (
        Path(env["GROOVEHOUND_CONFIG"]) if "GROOVEHOUND_CONFIG" in env else None
    )
    if explicit is not None:
        if not explicit.is_file():
            raise FileNotFoundError(f"config file not found: {explicit}")
        candidates: tuple[Path, ...] = (explicit,)
    else:
        candidates = DEFAULT_CONFIG_PATHS
    for candidate in candidates:
        if candidate.is_file():
            with candidate.open("rb") as f:
                settings = _from_toml(tomllib.load(f), settings)
            break

    env_map = {
        "GROOVEHOUND_DB": ("database_path", Path),
        "GROOVEHOUND_CONTACT": ("contact", str),
        "GROOVEHOUND_MUSICBRAINZ_API_ROOT": ("musicbrainz_api_root", str),
        "GROOVEHOUND_MUSICBRAINZ_INTERVAL": ("musicbrainz_interval", float),
        "GROOVEHOUND_LISTENBRAINZ_USER": ("listenbrainz_user", str),
        "GROOVEHOUND_LISTENBRAINZ_TOKEN": ("listenbrainz_token", str),
        "GROOVEHOUND_QOBUZ_STORE": ("qobuz_store", str),
        "GROOVEHOUND_LIBRARY": ("library_path", Path),
        "GROOVEHOUND_FOLLOW_TYPES": ("follow_types", _types),
    }
    for var, (attr, convert) in env_map.items():
        if env.get(var):
            settings = replace(settings, **{attr: convert(env[var])})

    return replace(settings, **{k: v for k, v in overrides.items() if v is not None})
