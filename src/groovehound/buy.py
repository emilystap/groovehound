"""Where to buy a wishlist item (roadmap 1c).

Two kinds of links, neither of which scrapes a store:

- **Store searches**, built locally from the artist and title (no request): Bandcamp and Qobuz.
- **Links MusicBrainz already has**: editors attach "purchase for download", "purchase for
  mail-order" and "streaming" URLs to releases, often straight to the Bandcamp or Qobuz page.
  One browse of the release group's releases covers every edition; it's shared with *Choose
  edition*, and made and cached as `releases.py` describes (only when opened).

The edition saved with the item, if any, has its links listed first. One link per store and
kind: a Bandcamp album page is the same whichever edition it's attached to.
"""

from __future__ import annotations

import random
import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime

from . import releases
from .brainz.http import ServicePause
from .brainz.musicbrainz import MusicBrainzClient
from .db import Store, WishlistItem

QOBUZ_STORE = re.compile(r"[a-z]{2}-[a-z]{2}")  # e.g. gb-en: country, then language
MAX_LINKS = 8  # per kind, so a release with dozens of streaming links stays readable

# MusicBrainz release-url relationship types -> (kind, note)
RELATIONSHIPS = {
    "purchase for download": ("buy", ""),
    "purchase for mail-order": ("buy", "physical"),
    "download for free": ("buy", "free"),
    "streaming": ("stream", ""),
    "free streaming": ("stream", "free"),
}

# Hosts (and their subdomains) -> the name shown; anything else shows its host name.
SITES = {
    "7digital.com": "7digital",
    "amazon.com": "Amazon",
    "amazon.co.uk": "Amazon",
    "amazon.de": "Amazon",
    "amazon.fr": "Amazon",
    "apple.com": "Apple Music",
    "bandcamp.com": "Bandcamp",
    "beatport.com": "Beatport",
    "boomkat.com": "Boomkat",
    "deezer.com": "Deezer",
    "hdtracks.com": "HDtracks",
    "junodownload.com": "Juno Download",
    "qobuz.com": "Qobuz",
    "soundcloud.com": "SoundCloud",
    "spotify.com": "Spotify",
    "tidal.com": "Tidal",
    "music.youtube.com": "YouTube Music",
    "youtube.com": "YouTube",
}


@dataclass(frozen=True)
class Link:
    site: str  # e.g. "Bandcamp"
    url: str
    kind: str  # "search", "buy" or "stream"
    note: str = ""  # e.g. "physical", "free"


@dataclass(frozen=True)
class WhereToBuy:
    """What an item's *Where to buy* shows. `links` is None until MusicBrainz has been asked;
    `due` = opening it should ask (again)."""

    searches: tuple[Link, ...]
    links: tuple[Link, ...] | None
    due: bool

    @property
    def buy(self) -> tuple[Link, ...]:
        return tuple(link for link in self.links or () if link.kind == "buy")

    @property
    def stream(self) -> tuple[Link, ...]:
        return tuple(link for link in self.links or () if link.kind == "stream")


def check_qobuz_store(store: str) -> str:
    if not QOBUZ_STORE.fullmatch(store):
        raise ValueError(f"qobuz store must look like us-en or gb-en: {store!r}")
    return store


def search_links(item: WishlistItem, qobuz_store: str) -> tuple[Link, ...]:
    """Store searches for the item's artist and title, built locally."""
    query = " ".join(p for p in (item.artist_credit, item.title) if p)
    q = urllib.parse.quote(query, safe="")
    return (
        Link(
            "Bandcamp",
            f"https://bandcamp.com/search?{urllib.parse.urlencode({'q': query, 'item_type': 'a'})}",
            "search",
        ),
        # qobuz.com/<store>/search?q= redirects here; asking for it directly saves a hop
        Link(
            "Qobuz",
            f"https://www.qobuz.com/{check_qobuz_store(qobuz_store)}/search/albums/{q}",
            "search",
        ),
    )


def where_to_buy(
    store: Store, item: WishlistItem, qobuz_store: str, *, now: datetime | None = None
) -> WhereToBuy:
    """What's known without a request: the store searches, and MusicBrainz's links if cached."""
    entry = releases.cached(store, item.release_group_mbid, now=now)
    return WhereToBuy(
        searches=search_links(item, qobuz_store),
        links=None if entry is None else musicbrainz_links(entry.data, item.release_mbid),
        due=entry is None or entry.due,
    )


def fetch_links(
    store: Store,
    client: MusicBrainzClient,
    pause: ServicePause,
    item: WishlistItem,
    *,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> tuple[Link, ...]:
    """MusicBrainz's purchase and streaming links for the item: from the cache while it's
    fresh, otherwise one request. Raises ServiceError as `releases.fetch` does."""
    data = releases.fetch(store, client, pause, item.release_group_mbid, now=now, rng=rng)
    return musicbrainz_links(data, item.release_mbid)


def musicbrainz_links(data: dict, release_mbid: str | None = None) -> tuple[Link, ...]:
    """Purchase and streaming links from a browse of a release group's releases: the chosen
    edition's first, then in MusicBrainz's order; one per site, kind and note; ended
    relationships left out."""
    releases = sorted(data.get("releases") or [], key=lambda r: r.get("id") != release_mbid)
    seen: set[tuple[str, str, str]] = set()
    counts = {"buy": 0, "stream": 0}
    links = []
    for release in releases:
        for relation in release.get("relations") or []:
            kind, note = RELATIONSHIPS.get(relation.get("type") or "", (None, ""))
            url = ((relation.get("url") or {}).get("resource") or "").strip()
            if kind is None or relation.get("ended") or not url.startswith(("https://", "http://")):
                continue  # (only web links: nothing else belongs in an href)
            site = site_name(url)
            if (site, kind, note) in seen or counts[kind] >= MAX_LINKS:
                continue
            seen.add((site, kind, note))
            counts[kind] += 1
            links.append(Link(site, url, kind, note))
    return tuple(links)


def site_name(url: str) -> str:
    """A store's name for a link: "Bandcamp" for artist.bandcamp.com, else the host name."""
    host = (urllib.parse.urlsplit(url).hostname or "").removeprefix("www.")
    for domain in sorted(SITES, key=len, reverse=True):  # music.youtube.com before youtube.com
        if host == domain or host.endswith("." + domain):
            return SITES[domain]
    return host
