"""FastAPI app: server-rendered Jinja templates, HTMX (vendored in static/) for partial updates.

Each request opens its own Store. Routes that act for a user get it from `current_user`, the
only place that decides who that is: today always the owner (`db.OWNER_ID`); with accounts,
the logged-in user from the session. While the app runs, `reconcile.Reconciler` re-browses
followed artists in the background (roadmap 2c), `covers_ahead.CoverFetcher` caches their covers
(roadmap 2e), and `library.LibraryScanner` scans the music folder, if there is one (roadmap 5a).
"""

from __future__ import annotations

import json
import mimetypes
from collections.abc import AsyncIterator, Iterable, Iterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs, urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import (
    __version__,
    album,
    artist,
    buy,
    covers,
    follows,
    history,
    library,
    library_albums,
    library_artists,
    listens,
    new_releases,
    personal,
    playlists,
    recommended,
    rediscover,
    releases,
    shelves,
    similar,
    tint,
    tracks,
)
from ..brainz import coverart, listenbrainz, musicbrainz
from ..brainz.http import NotFound, ServiceError, validate_contact
from ..brainz.musicbrainz import is_mbid
from ..config import Settings
from ..covers_ahead import CoverFetcher
from ..db import OWNER_ID, Store, User, WishlistItem, local_date, local_time, utcnow
from ..reconcile import Reconciler
from ..search import KINDS, clean, search
from ..share import parse_share

MAX_NOTES = 2000  # characters of a wishlist item's notes
MAX_CAPTURE = 500  # characters of a quick capture
COVER_MAX_AGE = 30 * 24 * 3600  # seconds the browser keeps a cover
NO_COVER_MAX_AGE = 24 * 3600  # ... or the answer that there's none

HERE = Path(__file__).parent

# Not in every system's MIME table: make sure the manifest is served as the type Chrome expects
mimetypes.add_type("application/manifest+json", ".webmanifest")


def get_store(request: Request) -> Iterator[Store]:
    """One Store (SQLite connection) per request."""
    with Store(request.app.state.settings.database_path) as store:
        yield store


StoreDep = Annotated[Store, Depends(get_store)]


def current_user(store: StoreDep) -> User:
    user = store.user(OWNER_ID)
    if user is None:
        raise HTTPException(500, "the owner account is missing from the database")
    return user


UserDep = Annotated[User, Depends(current_user)]


def create_app(settings: Settings) -> FastAPI:
    """Build the app. Raises ValueError for invalid MusicBrainz settings (a bad api_root, or an
    interval below the limit for musicbrainz.org), a bad Qobuz store or follow types."""
    if settings.contact:
        musicbrainz.client_for(settings)  # validates contact, api_root and interval at startup
    buy.check_qobuz_store(settings.qobuz_store)
    new_follow_types = follows.clean_types(list(settings.follow_types))

    # The slow re-browse of followed artists (roadmap 2c), only once MusicBrainz can be asked
    reconciler = None if _contact_problem(settings) else Reconciler(settings)
    # Their covers ahead of time (roadmap 2e), likewise only with a contact
    cover_fetcher = None if _contact_problem(settings) else CoverFetcher(settings)
    # The library scan (roadmap 5a): no requests, so it runs with or without a contact
    scanner = library.LibraryScanner(settings) if settings.library_path else None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        workers = [w for w in (reconciler, cover_fetcher, scanner) if w]
        for worker in workers:
            worker.start()
        try:
            yield
        finally:
            for worker in workers:
                worker.shutdown()

    app = FastAPI(
        title="Groovehound", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan
    )
    app.state.settings = settings
    app.state.scanner = scanner
    with Store(settings.database_path):
        pass  # run migrations at startup, not on the first request

    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(
        version=__version__,
        settings=settings,
        musicbrainz_web=musicbrainz.WEB_ROOT,
        listenbrainz_web=listenbrainz.WEB_ROOT,
    )
    templates.env.filters["local_date"] = local_date  # stored UTC, shown in the TZ time zone
    templates.env.filters["local_time"] = local_time
    templates.env.filters["duration"] = _duration
    templates.env.filters["out_label"] = _out_label
    templates.env.globals["followable"] = follows.credited  # a credit's *Follow* buttons
    templates.env.globals["credit_links"] = follows.credit_links  # names linked to artist pages
    templates.env.globals.update(  # a follow's release group types (_follow_types.html)
        primary_types=follows.PRIMARY_TYPES,
        secondary_types=follows.SECONDARY_TYPES,
        types_label=follows.types_label,
    )
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz() -> str:
        return "ok"

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, store: StoreDep, user: UserDep) -> HTMLResponse:
        return home_page(request, store, user)

    def home_page(request: Request, store: Store, user: User, draft: str = "") -> HTMLResponse:
        """The home page (no requests); with `draft`, the quick capture form is filled in."""
        lb_user = _listenbrainz_user(user)
        found = shelves.home(store, user.id, lb_user)
        return templates.TemplateResponse(
            request,
            "home.html",
            {
                "page": "home",
                "user": user,
                "contact_problem": _contact_problem(settings),
                "shelves": found,
                "follow_count": len(store.follows(user.id)),
                "captures": store.captures(user.id),
                "listenbrainz_user": lb_user,
                **tiles_context(store, user, shelves.all_tiles(found)),
                "draft": draft,
                "here": "/",
            },
        )

    @app.get("/shelves/next", response_class=HTMLResponse)
    def shuffle_next(request: Request, store: StoreDep, user: UserDep) -> Response:
        """*Shuffle*: Put on next drawn again (no requests). HTMX gets the shelf; a plain
        request goes back to it on the home page, which draws again anyway."""
        if not request.headers.get("HX-Request"):
            return RedirectResponse("/#next", status_code=303)
        shelf = shelves.put_on_next(store, user.id, _listenbrainz_user(user))
        if shelf is None:
            return HTMLResponse("")
        return templates.TemplateResponse(
            request,
            "_shelf.html",
            {"shelf": shelf, **tiles_context(store, user, shelf.tiles), "here": "/"},
        )

    @app.get("/new", response_class=HTMLResponse)
    def new_page(request: Request, store: StoreDep, user: UserDep) -> HTMLResponse:
        """Home's new release shelves, whole, as grids (no requests)."""
        lb_user = _listenbrainz_user(user)
        sections = shelves.new_page(store, user.id, lb_user)
        return templates.TemplateResponse(
            request,
            "new.html",
            {
                "page": "new",
                "sections": sections,
                "follow_count": len(store.follows(user.id)),
                "listenbrainz_user": lb_user,
                **tiles_context(store, user, shelves.all_tiles(sections)),
                "here": "/new",
            },
        )

    @app.get("/discover", response_class=HTMLResponse)
    def discover(request: Request, store: StoreDep, user: UserDep) -> HTMLResponse:
        """Albums ListenBrainz recommends, and the playlists it created for you, as albums (no
        requests)."""
        lb_user = _listenbrainz_user(user)
        found = playlists.playlists(store, user.id, lb_user)
        recommendations = recommended.recommended(store, user.id, lb_user)
        picked = shelves.songs(recommendations.albums if recommendations else ())
        lists = [(p, shelves.playlist_key(p), shelves.songs(p.releases)) for p in found]
        return templates.TemplateResponse(
            request,
            "discover.html",
            {
                "page": "discover",
                "user": user,
                "listenbrainz_user": lb_user,
                "playlists": lists,
                "recommended": recommendations,
                "recommended_tiles": picked,
                **tiles_context(store, user, [*picked, *(t for _, _, ts in lists for t in ts)]),
                "here": "/discover",
            },
        )

    @app.get("/rediscover", response_class=HTMLResponse)
    def rediscover_page(
        request: Request, store: StoreDep, user: UserDep, draw: str = ""
    ) -> HTMLResponse:
        """Albums to put on next (no requests): the draw `draw` names (Home's shelf, reloaded
        or come back to), else a new one."""
        lb_user = _listenbrainz_user(user)
        drawn = rediscover.draw(store, user.id, lb_user, seed=draw, per_section=rediscover.SEE_ALL)
        sections = shelves.rediscover_sections(drawn, local_date(utcnow())) if drawn else []
        return templates.TemplateResponse(
            request,
            "rediscover.html",
            {
                "page": "rediscover",
                "user": user,
                "listenbrainz_user": lb_user,
                "draw": drawn,
                "sections": sections,
                "library_count": store.library_summary(user.id).release_groups,
                **tiles_context(store, user, [t for _, _, ts in sections for t in ts]),
                "here": f"/rediscover?draw={drawn.seed}" if drawn is not None else "/rediscover",
            },
        )

    @app.post("/rediscover/{mbid}/dismiss")
    async def dismiss_rediscover(
        request: Request, mbid: str, store: StoreDep, user: UserDep
    ) -> Response:
        """Stop drawing a release group for Rediscover. HTMX gets an empty swap, removing it
        from the page; a plain form post goes back to `next`."""
        if not is_mbid(mbid):
            raise HTTPException(404, "not a release group MBID")
        form = await _form(request)
        store.dismiss(user.id, rediscover.DISMISSED_KIND, mbid.strip().lower())
        if request.headers.get("HX-Request"):
            return HTMLResponse("")
        return RedirectResponse(_local_path(form.get("next", "/rediscover")), status_code=303)

    def owned_context(store: Store, user: User, mbids: Iterable[str]) -> dict[str, object]:
        """Which of these release groups are in the library (5b), which of those were never
        played (5e), and which you have only some tracks of (5k), for `_owned.html`."""
        owned = store.in_library(user.id, mbids)
        return {
            "owned": owned,
            "unplayed": listens.never_played(store, user.id, _listenbrainz_user(user), owned),
            "partial": tracks.partial_albums(store, user.id, owned),
        }

    def tiles_context(store: Store, user: User, tiles: Iterable[shelves.Tile]) -> dict[str, object]:
        """What album tiles need besides themselves, for their badges and ⋯ menus."""
        tiles = list(tiles)
        mbids = [t.mbid for t in tiles]
        return {
            "followed": store.followed(user.id, (a.mbid for t in tiles for a in t.credits)),
            "saved_ids": store.wishlisted(user.id, mbids),
            **owned_context(store, user, mbids),
        }

    def _listenbrainz_user(user: User) -> str:
        """The user's ListenBrainz username: from config, for the owner only, until per-user
        settings are editable (roadmap "Users")."""
        return settings.listenbrainz_user.strip() if user.id == OWNER_ID else ""

    @app.post("/new/{mbid}/dismiss")
    async def dismiss_new_release(
        request: Request, mbid: str, store: StoreDep, user: UserDep
    ) -> Response:
        """Stop showing a release group among the new releases. HTMX gets an empty swap,
        removing it from the page; a plain form post goes back to `next`."""
        if not is_mbid(mbid):
            raise HTTPException(404, "not a release group MBID")
        form = await _form(request)
        store.dismiss(user.id, new_releases.DISMISSED_KIND, mbid.strip().lower())
        if request.headers.get("HX-Request"):
            return HTMLResponse("")
        return RedirectResponse(_local_path(form.get("next", "/")), status_code=303)

    @app.get("/cover/{mbid}")
    def cover(mbid: str, store: StoreDep, user: UserDep) -> Response:
        """A cover thumbnail from the cover cache (made from your library's files if they have
        art, else one request to the Cover Art Archive the first time, or when due for a
        refresh). Only for release groups on your wishlist, by artists you follow, in your
        ListenBrainz fresh releases (the home page), in the playlists it created for you or its
        recommendations (Discover), in your library, an album whose page you opened (its
        editions are cached), or already cached (an artist's picture can show a cover from any
        of those that has since left), so this isn't a way to make the server fetch anything
        else."""
        mbid = mbid.lower()
        if not is_mbid(mbid) or not (
            store.wishlisted(user.id, [mbid])
            or store.followed_release_group(user.id, mbid)
            or store.in_library(user.id, [mbid])
            or personal.listed(store, _listenbrainz_user(user), mbid)
            or playlists.listed(store, _listenbrainz_user(user), mbid)
            or recommended.listed(store, _listenbrainz_user(user), mbid)
            or releases.cached(store, mbid) is not None
            or store.cached_covers([mbid])
        ):
            raise HTTPException(404, "not on your wishlist, home page, Discover or in your library")
        client = None if _contact_problem(settings) else coverart.client_for(settings)
        library_root = settings.library_path if user.id == OWNER_ID else None
        try:
            found = covers.cover(
                store, client, coverart.PAUSE, mbid, library_root=library_root, user_id=user.id
            )
        except ServiceError:
            return Response(status_code=503, headers={"Cache-Control": "no-store"})
        if found.image is None:
            return Response(
                status_code=404, headers={"Cache-Control": f"max-age={NO_COVER_MAX_AGE}"}
            )
        return Response(
            found.image,
            media_type=found.content_type,
            headers={
                "Cache-Control": f"private, max-age={COVER_MAX_AGE}",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/albums/{mbid}", response_class=HTMLResponse)
    def album_page(
        request: Request, mbid: str, store: StoreDep, user: UserDep, edition: str = ""
    ) -> HTMLResponse:
        """An album (release group): its header from what's stored, the track list of an
        edition (`edition`, else the one you have or want, else the default) if cached, else
        loaded after the page shows (/albums/<mbid>/tracks), your copy, the wishlist item, its
        editions and where to buy. A request only for an album nothing stored knows: the
        editions browse, which names it too."""
        mbid = _release_group_mbid(mbid)
        if album.album(store, user.id, _listenbrainz_user(user), mbid) is None:
            problem = _contact_problem(settings)
            if problem:
                raise HTTPException(404, f"not an album Groovehound knows yet ({problem})")
            try:
                releases.fetch(store, musicbrainz.client_for(settings), musicbrainz.PAUSE, mbid)
            except ServiceError as exc:
                raise HTTPException(503, str(exc)) from exc
        context = album_context(request, store, user, mbid, edition)
        if context is None:
            raise HTTPException(404, "no such album on MusicBrainz")
        return templates.TemplateResponse(request, "album.html", context)

    @app.get("/albums/{mbid}/tracks", response_class=HTMLResponse)
    def album_tracks(
        request: Request, mbid: str, store: StoreDep, user: UserDep, edition: str = ""
    ) -> Response:
        """An album's track list, asking MusicBrainz for what isn't cached (its editions, if
        the edition isn't known without them, then the edition's tracks: at most two
        requests). HTMX gets the track list in place; without JavaScript, back to the page."""
        mbid = _release_group_mbid(mbid)
        error = _contact_problem(settings)
        if not error:
            try:
                album.fetch_track_list(
                    store,
                    musicbrainz.client_for(settings),
                    musicbrainz.PAUSE,
                    user.id,
                    mbid,
                    store.wishlist_item_of(user.id, mbid),
                    _edition(edition),
                )
            except ServiceError as exc:
                error = str(exc)
        return album_part_or_back(request, store, user, mbid, edition, "_tracks.html", error)

    @app.get("/albums/{mbid}/editions", response_class=HTMLResponse)
    def album_editions(
        request: Request, mbid: str, store: StoreDep, user: UserDep, edition: str = ""
    ) -> Response:
        """Every edition of an album, asked for when its *Editions* is opened: one request
        unless cached (the browse *Where to buy* uses too). HTMX gets the list in place."""
        mbid = _release_group_mbid(mbid)
        error = fetch_album_releases(store, mbid)
        return album_part_or_back(
            request, store, user, mbid, edition, "_album_editions.html", error
        )

    @app.get("/albums/{mbid}/links", response_class=HTMLResponse)
    def album_links(
        request: Request, mbid: str, store: StoreDep, user: UserDep, edition: str = ""
    ) -> Response:
        """MusicBrainz's purchase and streaming links for an album, asked for when its *Where
        to buy* is opened: one request unless cached. HTMX gets them in place."""
        mbid = _release_group_mbid(mbid)
        error = fetch_album_releases(store, mbid)
        return album_part_or_back(request, store, user, mbid, edition, "_buy_links.html", error)

    def album_context(
        request: Request,
        store: Store,
        user: User,
        mbid: str,
        edition: str = "",
        error: str | None = None,
    ) -> dict[str, object] | None:
        """What the album page and its parts show (no requests); None if nothing stored knows
        the album."""
        found = album.album(store, user.id, _listenbrainz_user(user), mbid)
        if found is None:
            return None
        chosen = _edition(edition)
        item = found.item
        # Where to buy and Editions work on an item, saved or not
        wanted = item or WishlistItem(mbid, found.hit.title, found.hit.artist_credit)
        here = f"/albums/{mbid}"
        query = f"?edition={chosen}" if chosen else ""
        return {
            "page": _tab_from(request),
            "mbid": mbid,
            "album": found,
            "hit": found.hit,
            "item": item,
            "edition": chosen,
            "tracks": album.track_list(store, user.id, mbid, item, chosen),
            "details": album.edition_details(store, mbid),
            "choice": releases.choice(store, wanted),
            "where": buy.where_to_buy(store, wanted, settings.qobuz_store),
            "found": library.found(store, user.id, [item]) if item else {},
            "followed": store.followed(user.id, found.hit.artist_mbids),
            "band": {
                "dark": tint.band(found.tint, dark=True),
                "light": tint.band(found.tint, dark=False),
            },
            "contact_problem": _contact_problem(settings),
            "error": error,
            "links_url": f"{here}/links{query}",
            "here": here + query,
        }

    def album_part_or_back(
        request: Request,
        store: Store,
        user: User,
        mbid: str,
        edition: str,
        part: str,
        error: str | None,
    ) -> Response:
        """After asking MusicBrainz for a part of an album page: HTMX gets the part, a plain
        request goes back to it on the page."""
        if not request.headers.get("HX-Request"):
            chosen = _edition(edition)
            query = f"?edition={chosen}" if chosen else ""
            anchor = {"_tracks.html": "tracks", "_album_editions.html": "editions"}.get(part, "buy")
            return RedirectResponse(f"/albums/{mbid}{query}#{anchor}", status_code=303)
        context = album_context(request, store, user, mbid, edition, error)
        if context is None:
            raise HTTPException(404, error or "no such album on MusicBrainz")
        return templates.TemplateResponse(request, part, {**context, "loaded": True})

    def fetch_album_releases(store: Store, mbid: str) -> str | None:
        """Make sure the album's editions are cached (one request if they aren't, or are
        due); why not, if that failed."""
        error = _contact_problem(settings)
        if not error:
            try:
                releases.fetch(store, musicbrainz.client_for(settings), musicbrainz.PAUSE, mbid)
            except ServiceError as exc:
                error = str(exc)
        return error

    @app.get("/search", response_class=HTMLResponse)
    def search_page(
        request: Request,
        store: StoreDep,
        user: UserDep,
        kind: str = "album",
        title: str = "",
        artist: str = "",
        capture: int | None = None,
    ) -> HTMLResponse:
        """The search form; with a title, one MusicBrainz search (or its cached answer). With
        `capture`, it's looking up that quick capture: its text is shown, and saving a result
        files it on the item."""
        return search_page_for(request, store, user, kind, title, artist, capture)

    @app.get("/share", response_class=HTMLResponse)
    def share(
        request: Request,
        store: StoreDep,
        user: UserDep,
        title: str = "",
        text: str = "",
        url: str = "",
    ) -> HTMLResponse:
        """Something shared to Groovehound from Android's share sheet (the manifest's
        `share_target`). "<song> by <artist>", as Now Playing shares it, gives the song search
        filled in; anything else, the quick capture form filled in. Nothing is searched or saved
        until you press the button, so a bad guess can be fixed first."""
        found = parse_share(title, text, url)
        if found:
            return search_page_for(request, store, user, "song", *found, run=False)
        draft = " ".join(" ".join((title, text, url)).split())[:MAX_CAPTURE]
        return home_page(request, store, user, draft)

    def search_page_for(
        request: Request,
        store: Store,
        user: User,
        kind: str,
        title: str,
        artist: str,
        capture: int | None = None,
        run: bool = True,
    ) -> HTMLResponse:
        """The search page; it searches only if `run` (and there's a title)."""
        kind = kind if kind in KINDS else "album"
        title, artist = clean(title), clean(artist)
        contact_problem = _contact_problem(settings)
        hits, error = None, None
        if run and title and not contact_problem:
            try:
                hits = search(
                    store,
                    musicbrainz.client_for(settings),
                    musicbrainz.PAUSE,
                    kind,
                    title,
                    artist,
                )
            except ServiceError as exc:
                error = str(exc)
        return templates.TemplateResponse(
            request,
            "search.html",
            {
                "page": "search",
                "kind": kind,
                "title": title,
                "artist": artist,
                "hits": hits,
                "error": error,
                "contact_problem": contact_problem,
                "capture": store.capture(user.id, capture) if capture is not None else None,
                "saved_ids": store.wishlisted(user.id, (h.mbid for h in hits or ())),
                **owned_context(store, user, (h.mbid for h in hits or ())),
                "followed": store.followed(
                    user.id, (a for h in hits or () for a in h.artist_mbids)
                ),
                "prefilled": not run,
                "here": str(request.url.path)
                + (f"?{request.url.query}" if request.url.query else ""),
            },
        )

    @app.post("/wishlist")
    async def add_to_wishlist(request: Request, store: StoreDep, user: UserDep) -> Response:
        """Save a search result. The fields are the result's own (no request is made); with
        `capture`, that quick capture is filed in the item's notes. An HTMX request gets the
        button's replacement, a plain form post goes back to `next`."""
        form = await _form(request)
        item = _wishlist_item(form)
        store.add_to_wishlist(user.id, item)
        filed = form.get("capture", "").isdigit() and store.file_capture(
            user.id, int(form["capture"]), item.release_group_mbid
        )
        if request.headers.get("HX-Request"):
            return templates.TemplateResponse(
                request,
                "_wishlist_button.html",
                {"mbid": item.release_group_mbid, "saved": True, "filed": filed},
            )
        return RedirectResponse(_local_path(form.get("next", "")), status_code=303)

    @app.get("/wishlist", response_class=HTMLResponse)
    def wishlist_page(
        request: Request, store: StoreDep, user: UserDep, show: str = ""
    ) -> HTMLResponse:
        """Everything still to buy, newest first; `?show=bought` lists the bought items and
        `?show=look-up` the quick captures waiting to be looked up."""
        show = show if show in ("bought", "look-up") else ""
        items = [] if show == "look-up" else store.wishlist(user.id, bought=show == "bought")
        return templates.TemplateResponse(
            request,
            "wishlist.html",
            {
                "page": "wishlist",
                "show": show,
                "items": items,
                "captures": store.captures(user.id) if show == "look-up" else [],
                **item_context(store, user, items),
                **_wishlist_counts(store, user),
                "here": str(request.url.path)
                + (f"?{request.url.query}" if request.url.query else ""),
            },
        )

    @app.post("/wishlist/{item_id}/notes")
    async def annotate_wishlist_item(
        request: Request, item_id: int, store: StoreDep, user: UserDep
    ) -> Response:
        """Save an item's notes and "where I heard it" (the album page's form)."""
        form = await _form(request)
        notes = _multiline(form.get("notes", ""))[:MAX_NOTES]
        if not store.annotate_wishlist_item(user.id, item_id, notes, clean(form.get("heard", ""))):
            raise HTTPException(404, "not on your wishlist")
        return RedirectResponse(_local_path(form.get("next", "/wishlist")), status_code=303)

    @app.post("/wishlist/{item_id}/edition")
    async def choose_edition(
        request: Request, item_id: int, store: StoreDep, user: UserDep
    ) -> Response:
        """Save which edition an item is for (`release`, empty = any edition; the album page's
        form). Its links then come first under *Where to buy*."""
        form = await _form(request)
        release = form.get("release", "").strip()
        if release and not is_mbid(release):
            raise HTTPException(400, "malformed release MBID")
        if not store.choose_edition(user.id, item_id, release.lower() or None):
            raise HTTPException(404, "not on your wishlist")
        return RedirectResponse(_local_path(form.get("next", "/wishlist")), status_code=303)

    @app.post("/wishlist/{item_id}/bought")
    async def mark_bought(
        request: Request, item_id: int, store: StoreDep, user: UserDep
    ) -> Response:
        """Mark an item bought (`bought=1`) or still wanted (`bought=0`). Either way it moves to
        the other list, so HTMX gets nothing back and the item leaves this one."""
        form = await _form(request)
        if not store.mark_bought(user.id, item_id, form.get("bought", "1") != "0"):
            raise HTTPException(404, "not on your wishlist")
        return gone_or_back(request, store, user, form)

    @app.post("/wishlist/{item_id}/remove")
    async def remove_from_wishlist(
        request: Request, item_id: int, store: StoreDep, user: UserDep
    ) -> Response:
        form = await _form(request)
        if not store.remove_from_wishlist(user.id, item_id):
            raise HTTPException(404, "not on your wishlist")
        return gone_or_back(request, store, user, form)

    @app.get("/artists", response_class=HTMLResponse)
    def artists_page(request: Request, store: StoreDep, user: UserDep) -> HTMLResponse:
        """The artists you follow (no requests)."""
        follows_ = store.follows(user.id)
        return templates.TemplateResponse(
            request,
            "artists.html",
            {
                "page": "library",
                "follows": follows_,
                "pictures": {
                    f.artist_mbid: artist.followed_picture(store, user.id, f) for f in follows_
                },
                **library_tabs(store, user),
                "here": "/artists",
            },
        )

    @app.get("/artists/library", response_class=HTMLResponse)
    def library_artists_page(request: Request, store: StoreDep, user: UserDep) -> HTMLResponse:
        """Every album artist in the library, with the suggested follows first (no requests)."""
        found = library_artists.library_artists(store, user.id, new_follow_types)
        suggested = library_artists.suggestions(store, user.id, found)
        return templates.TemplateResponse(
            request,
            "artists_library.html",
            {
                "page": "library",
                "artists": found,
                "suggestions": suggested,
                "suggest_at": library_artists.SUGGEST_AT,
                "pictures": artist.library_pictures(store, user.id, found),
                "followed": store.followed(user.id, (a.mbid for a in suggested)),
                **library_tabs(store, user),
                "here": "/artists/library",
            },
        )

    @app.post("/artists/{mbid}/dismiss-suggestion")
    async def dismiss_suggested_follow(
        request: Request, mbid: str, store: StoreDep, user: UserDep
    ) -> Response:
        """Stop suggesting an artist to follow (5d). HTMX gets an empty swap, removing it."""
        mbid = _artist_mbid(mbid)
        form = await _form(request)
        store.dismiss(user.id, library_artists.DISMISSED_KIND, mbid)
        if request.headers.get("HX-Request"):
            return HTMLResponse("")
        return RedirectResponse(_local_path(form.get("next", "/artists/library")), status_code=303)

    @app.get("/artists/{mbid}", response_class=HTMLResponse)
    def artist_page(
        request: Request, mbid: str, store: StoreDep, user: UserDep, missing: str = ""
    ) -> HTMLResponse:
        """An artist's albums and EPs, with *Follow*: one browse unless cached (the same one
        following makes, so following from here asks nothing more). For an artist in the
        library, what you have of it and what's missing (5j; `missing=1`: only what's missing),
        which browses their singles too when the types counted include them."""
        mbid = _artist_mbid(mbid)
        contact_problem = _contact_problem(settings)
        follow = store.follow_of(user.id, mbid)
        types = follow.types if follow else new_follow_types
        library_releases = store.library_of_artist(user.id, mbid)
        in_library = bool(library_releases)
        found, error = None, None
        if contact_problem:
            found, _ = follows.cached_discography(store, mbid)
        else:
            client = musicbrainz.client_for(settings)
            try:
                found = follows.discography(store, client, musicbrainz.PAUSE, mbid)
                # followed for singles (keep them fresh), or counting them as missing
                if "single" in types and (follow or in_library):
                    found = replace(
                        found, singles=follows.singles(store, client, musicbrainz.PAUSE, mbid)
                    )
            except NotFound as exc:
                raise HTTPException(404, "no such artist on MusicBrainz") from exc
            except ServiceError as exc:
                found, _ = follows.cached_discography(store, mbid)
                error = str(exc)
        hits = (*found.release_groups, *(found.singles or ())) if found else ()
        gaps = library_artists.gaps(store, user.id, mbid, found, types) if found else None
        name = found.artist.name if found else follow.name if follow else ""
        if not name and library_releases:  # MusicBrainz couldn't be asked: as the files name them
            name = library_artists.names(library_releases, None, None)[0]
        # the picture and the line under the name are of all of it, even with Missing only on
        shown = artist.picture(store, name, artist.release_groups(found, library_releases))
        about = artist.summary(found)
        mbids = [h.mbid for h in hits]
        served = None
        if not follow:
            served = {
                *store.cached_covers(mbids),
                *store.in_library(user.id, mbids),
                *store.wishlisted(user.id, mbids),
            }
        missing_only = bool(missing) and gaps is not None
        if missing_only:
            found = library_artists.missing_only(found, gaps)
        today = local_date(utcnow())
        main, other, singles = (
            artist.tiles(group, mbid, today, served)
            for group in (
                found.main if found else (),
                found.other if found else (),
                (found.singles or ()) if found else (),
            )
        )
        album_context = tiles_context(store, user, (*main, *other, *singles))
        similar_found = similar_context(store, user, mbid)
        return templates.TemplateResponse(
            request,
            "artist.html",
            {
                "page": "artists",
                "mbid": mbid,
                "found": found,
                # what to call them when MusicBrainz couldn't be asked: the name as followed
                "name": name,
                "about": about,
                "picture": shown,
                "band": {
                    "dark": tint.band(shown.tint, dark=True),
                    "light": tint.band(shown.tint, dark=False),
                },
                "main": main,
                "other": other,
                "singles": singles,
                "following": follow is not None,
                "follow": follow,
                "sheet": True,
                "error": error,
                "contact_problem": contact_problem,
                "gaps": gaps,
                "types_counted": types,
                "missing_only": missing_only,
                **album_context,
                **similar_found,
                "followed": album_context["followed"] | similar_found["followed"],
                "here": f"/artists/{mbid}",
            },
        )

    def similar_context(
        store: Store, user: User, mbid: str, error: str | None = None
    ) -> dict[str, object]:
        """What the artist page's *Similar artists* card shows (no requests)."""
        found = similar.cached(store, user.id, mbid)
        artists = found.artists if found else ()
        return {
            "similar": found,
            "similar_error": error,
            "similar_pictures": artist.pictures(store, user.id, artists),
            "followed": store.followed(user.id, (a.mbid for a in artists)),
        }

    @app.post("/artists/{mbid}/similar")
    async def similar_artists(
        request: Request, mbid: str, store: StoreDep, user: UserDep
    ) -> Response:
        """Ask ListenBrainz for artists like this one: one request, unless the cached answer is
        still fresh. HTMX gets the *Similar artists* card; a plain form post goes back to the
        artist page."""
        mbid = _artist_mbid(mbid)
        form = await _form(request)
        error = _contact_problem(settings)
        if not error:
            try:
                similar.fetch(store, listenbrainz.client_for(settings), listenbrainz.PAUSE, mbid)
            except ServiceError as exc:
                error = str(exc)
        if not request.headers.get("HX-Request"):
            return RedirectResponse(f"/artists/{mbid}#similar", status_code=303)
        follow = store.follow_of(user.id, mbid)
        return templates.TemplateResponse(
            request,
            "_similar.html",
            {
                "mbid": mbid,
                "name": follow.name if follow else clean(form.get("name", "")),
                "contact_problem": _contact_problem(settings),
                **similar_context(store, user, mbid, error),
                "here": f"/artists/{mbid}",
            },
        )

    @app.post("/artists/{mbid}/dismiss")
    async def dismiss_artist(
        request: Request, mbid: str, store: StoreDep, user: UserDep
    ) -> Response:
        """Stop suggesting an artist as similar to others. HTMX gets an empty swap, removing it
        from the page; a plain form post goes back to `next`."""
        mbid = _artist_mbid(mbid)
        form = await _form(request)
        store.dismiss(user.id, similar.DISMISSED_KIND, mbid)
        if request.headers.get("HX-Request"):
            return HTMLResponse("")
        return RedirectResponse(_local_path(form.get("next", "/artists")), status_code=303)

    @app.post("/artists/{mbid}/follow")
    async def follow_artist(
        request: Request, mbid: str, store: StoreDep, user: UserDep
    ) -> Response:
        """Follow an artist for the configured types: the first time, one browse of their albums
        and EPs (unless cached), and one of their singles if those are followed by default, so
        what's already out isn't reported as new. HTMX gets every *Follow* button for them on
        the page updated; a plain form post goes back to `next`."""
        mbid = _artist_mbid(mbid)
        form = await _form(request)
        error = _contact_problem(settings)
        if not error:
            try:
                follows.follow(
                    store,
                    musicbrainz.client_for(settings),
                    musicbrainz.PAUSE,
                    user.id,
                    mbid,
                    new_follow_types,
                )
            except NotFound as exc:
                raise HTTPException(404, "no such artist on MusicBrainz") from exc
            except ServiceError as exc:
                error = str(exc)
        return follow_button_or_back(request, store, user, mbid, form, error)

    @app.post("/artists/{mbid}/unfollow")
    async def unfollow_artist(
        request: Request, mbid: str, store: StoreDep, user: UserDep
    ) -> Response:
        """Stop following an artist. What's cached about them stays (it's shared)."""
        mbid = _artist_mbid(mbid)
        form = await _form(request)
        store.unfollow(user.id, mbid)
        return follow_button_or_back(request, store, user, mbid, form)

    def follow_button_or_back(
        request: Request,
        store: Store,
        user: User,
        mbid: str,
        form: dict[str, str],
        error: str | None = None,
    ) -> Response:
        """HTMX gets the artist's *Follow* button, out of band for every copy on the page (an
        artist can be on several results), and on the artist pages their types form too; a
        plain form post goes back to `next`."""
        if request.headers.get("HX-Request"):
            follow = store.follow_of(user.id, mbid)
            name = follow.name if follow else clean(form.get("name", "")) or mbid
            here = form.get("next", "")
            return templates.TemplateResponse(
                request,
                "_follow_button.html",
                {
                    "oob": True,
                    "artist": follows.Credited(mbid, name),
                    "following": follow is not None,
                    "follow": follow,
                    # only pages with a types form (else htmx reports a missing target)
                    "with_types": here in ("/artists", f"/artists/{mbid}"),
                    "link": here != f"/artists/{mbid}",  # not to the page it's on
                    "error": error,
                    "here": here,
                },
            )
        return RedirectResponse(_local_path(form.get("next", f"/artists/{mbid}")), status_code=303)

    @app.post("/artists/{mbid}/types")
    async def follow_types(request: Request, mbid: str, store: StoreDep, user: UserDep) -> Response:
        """Change which release group types a follow is for (`type`, repeated). Turning singles
        on browses them (one request unless cached), so the ones already out aren't new; on the
        artist page, HTMX then reloads it to list them."""
        mbid = _artist_mbid(mbid)
        fields = await _form_lists(request)
        here = (fields.get("next") or [""])[0]
        follow = store.follow_of(user.id, mbid)
        if follow is None:
            raise HTTPException(404, "you don't follow this artist")
        had_singles = "single" in follow.types
        error = None
        try:
            types = follows.clean_types(fields.get("type", []))
        except ValueError as exc:
            error = str(exc)
        if not error and "single" in types and not had_singles:
            error = _contact_problem(settings)
        if not error:
            try:
                follow = (
                    follows.set_types(
                        store,
                        musicbrainz.client_for(settings),
                        musicbrainz.PAUSE,
                        user.id,
                        mbid,
                        types,
                    )
                    or follow
                )
            except ServiceError as exc:
                error = str(exc)
        if not request.headers.get("HX-Request"):
            return RedirectResponse(_local_path(here or f"/artists/{mbid}"), status_code=303)
        headers = {}
        if not error and not had_singles and follow.singles_since and here == f"/artists/{mbid}":
            headers["HX-Refresh"] = "true"  # singles just turned on: the page lists them
        return templates.TemplateResponse(
            request,
            "_follow_types.html",
            {
                "mbid": mbid,
                "follow": follow,
                "error": error,
                "saved": not error,
                "here": here,
            },
            headers=headers,
        )

    @app.get("/library", response_class=HTMLResponse)
    def library_page(request: Request, store: StoreDep, user: UserDep) -> HTMLResponse:
        """The albums that want attention, then all of them (no requests)."""
        lb_user = _listenbrainz_user(user)
        found = library_albums.library(store, user.id, lb_user)
        today = local_date(utcnow())
        shown = library_albums.page(found)
        tiles = shelves.library_tiles(shown.albums, found, today)
        shelf_list = shelves.library(found, today)
        return templates.TemplateResponse(
            request,
            "library.html",
            {
                "page": "library",
                "shelves": shelf_list,
                "grid": shown,
                "tiles": tiles,
                "section": "all",
                "listenbrainz_user": lb_user,
                "stats": listens.stats(store, lb_user),
                **library_tabs(store, user),
                **library_tiles_context(store, user, [*shelves.all_tiles(shelf_list), *tiles]),
                "here": "/library",
            },
        )

    @app.get("/library/albums", response_class=HTMLResponse)
    def library_albums_page(
        request: Request,
        store: StoreDep,
        user: UserDep,
        filter: str = "",
        sort: str = "",
        page: int = 1,
    ) -> HTMLResponse:
        """*All albums*, filtered and sorted (no requests). HTMX, scrolling down, gets the
        next page's tiles alone."""
        lb_user = _listenbrainz_user(user)
        found = library_albums.library(store, user.id, lb_user)
        shown = library_albums.page(found, filter, sort, page)
        today = local_date(utcnow())
        tiles = shelves.library_tiles(shown.albums, found, today, shown.filter_key)
        context = {
            "grid": shown,
            "tiles": tiles,
            "section": "all",
            **library_tiles_context(store, user, tiles),
            "here": shown.url,
        }
        if request.headers.get("HX-Request"):
            return templates.TemplateResponse(request, "_album_page.html", context)
        never_played = shown.filter_key == library_albums.NEVER_PLAYED
        return templates.TemplateResponse(
            request,
            "library_albums.html",
            {
                "page": "library",
                "listenbrainz_user": lb_user,
                "stats": listens.stats(store, lb_user),
                "unplayed_artists": (
                    listens.unplayed_by_artist(store, user.id, lb_user) if never_played else []
                ),
                **context,
            },
        )

    @app.get("/library/scan", response_class=HTMLResponse)
    def library_scan_page(request: Request, store: StoreDep, user: UserDep) -> HTMLResponse:
        """What the library scan found, and what it couldn't use (no requests)."""
        return templates.TemplateResponse(
            request,
            "library_scan.html",
            {
                "page": "library",
                **library_status(store, user),
                "problems": library.problems(store, user.id),
                "history": history.status(store, user.id, _listenbrainz_user(user)),
                "here": "/library/scan",
            },
        )

    def library_tabs(store: Store, user: User) -> dict[str, object]:
        """What the Library tab's pages show at the top: its segments and the way to Scan."""
        return {
            "follow_count": len(store.follows(user.id)),
            "library_count": library_artists.count(store, user.id),
            "scan_problems": library.problems(store, user.id).count,
        }

    def library_tiles_context(
        store: Store, user: User, tiles: Iterable[shelves.Tile]
    ) -> dict[str, object]:
        """What library tiles need besides themselves: their badges are their own."""
        return {"followed": store.followed(user.id, (a.mbid for t in tiles for a in t.credits))}

    @app.get("/library/status", response_class=HTMLResponse)
    def library_status_part(request: Request, store: StoreDep, user: UserDep) -> HTMLResponse:
        """The Scan page's status, which polls itself while a scan runs."""
        return templates.TemplateResponse(
            request, "_library_status.html", library_status(store, user)
        )

    @app.post("/library/scan")
    async def rescan_library(request: Request, store: StoreDep, user: UserDep) -> Response:
        """Scan the whole library folder now, in the background."""
        if scanner is None or user.id != OWNER_ID:
            raise HTTPException(404, "no library folder configured")
        scanner.request()
        if request.headers.get("HX-Request"):
            return library_status_part(request, store, user)
        return RedirectResponse("/library/scan", status_code=303)

    def library_status(store: Store, user: User) -> dict[str, object]:
        """The library folder (the owner's, from config, until per-user settings exist), its
        latest scan and what's in it."""
        owner = user.id == OWNER_ID
        return {
            "library_path": settings.library_path if owner else None,
            "scanning": bool(owner and scanner and scanner.scanning),
            "scan": store.library_scan(user.id),
            "summary": store.library_summary(user.id),
        }

    @app.post("/captures")
    async def add_capture(request: Request, store: StoreDep, user: UserDep) -> Response:
        """Save free text to look up later. HTMX gets the quick capture card back (an empty
        form and the updated list), or, from the search page (`from=search`), a note that it's
        saved; a plain form post goes back to `next`."""
        form = await _form(request)
        text = " ".join(form.get("text", "").split())[:MAX_CAPTURE]
        if not text:
            raise HTTPException(400, "nothing to save")
        store.add_capture(user.id, text)
        if request.headers.get("HX-Request") and form.get("from") == "search":
            return templates.TemplateResponse(
                request, "_capture_search.html", {"saved": True, "text": text}
            )
        return captures_or_back(request, store, user, form)

    @app.post("/captures/{capture_id}/remove")
    async def remove_capture(
        request: Request, capture_id: int, store: StoreDep, user: UserDep
    ) -> Response:
        form = await _form(request)
        if not store.remove_capture(user.id, capture_id):
            raise HTTPException(404, "not one of your captures")
        return captures_or_back(request, store, user, form)

    def captures_or_back(
        request: Request, store: Store, user: User, form: dict[str, str]
    ) -> Response:
        if request.headers.get("HX-Request"):
            return templates.TemplateResponse(
                request,
                "_captures.html",
                {
                    "captures": store.captures(user.id),
                    "here": form.get("next", ""),
                    "tabs_oob": form.get("next", "").startswith("/wishlist"),
                    **_wishlist_counts(store, user),
                },
            )
        return RedirectResponse(_local_path(form.get("next", "/")), status_code=303)

    def item_context(store: Store, user: User, items: list[WishlistItem]) -> dict[str, object]:
        """What wishlist rows show without a request: the chosen edition and whether it's in
        the library by item id, its badges, and which of their artists are followed."""
        return {
            "editions": {i.id: releases.choice(store, i) for i in items},
            "followed": store.followed(user.id, (a for i in items for a in i.artist_mbids)),
            "found": library.found(store, user.id, items),
            "today": local_date(utcnow()),
            **owned_context(store, user, [i.release_group_mbid for i in items]),
        }

    def gone_or_back(request: Request, store: Store, user: User, form: dict[str, str]) -> Response:
        """After an item left the list it was on: HTMX gets an empty swap (removing it from
        the page) and the list counts out of band; a plain form post goes back to `next`."""
        if request.headers.get("HX-Request"):
            return templates.TemplateResponse(
                request,
                "_wishlist_tabs.html",
                {
                    "oob": True,
                    # which list the page shows: the one in `next`, i.e. the page's own URL
                    "show": parse_qs(urlsplit(form.get("next", "")).query).get("show", [""])[0],
                    **_wishlist_counts(store, user),
                },
            )
        return RedirectResponse(_local_path(form.get("next", "/wishlist")), status_code=303)

    return app


def _wishlist_counts(store: Store, user: User) -> dict[str, int]:
    return {
        "wanted_count": store.wishlist_count(user.id),
        "bought_count": store.wishlist_count(user.id, bought=True),
        "capture_count": len(store.captures(user.id)),
    }


async def _form(request: Request) -> dict[str, str]:
    """A urlencoded form's fields (first value each). Parsed by hand, as in tagdrifter, to
    avoid the python-multipart dependency for a few small forms."""
    return {k: v[0] for k, v in (await _form_lists(request)).items()}


async def _form_lists(request: Request) -> dict[str, list[str]]:
    """A urlencoded form's fields with every value (checkboxes sharing a name)."""
    return parse_qs((await request.body()).decode(errors="replace"))


def _release_group_mbid(mbid: str) -> str:
    """A release group MBID from the URL, lowercased. Raises HTTPException(404) if it isn't
    one."""
    if not is_mbid(mbid):
        raise HTTPException(404, "not a release group MBID")
    return mbid.strip().lower()


def _edition(edition: str) -> str | None:
    """A release MBID from `?edition=`, lowercased; None if there's none (or it isn't one)."""
    return edition.strip().lower() if is_mbid(edition) else None


def _tab_from(request: Request) -> str:
    """The destination an album page belongs to: the one it was opened from (the referring
    page), else Home. Discover and Rediscover belong to Home, artist pages to Library."""
    path = urlsplit(request.headers.get("referer", "")).path
    first = path.strip("/").split("/")[0]
    tabs = {"search": "search", "share": "search", "wishlist": "wishlist", "artists": "library"}
    return tabs.get(first, "library" if first == "library" else "home")


def _duration(ms: int | None) -> str:
    """A track's length as players show it: "3:07", "1:02:15"; "" if unknown."""
    if not ms:
        return ""
    minutes, seconds = divmod(round(ms / 1000), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"


def _out_label(date: str) -> str:
    """A release date for a badge: "Out 14 Nov"."""
    return shelves.out_label(date, local_date(utcnow()))


def _artist_mbid(mbid: str) -> str:
    """An artist MBID from the URL, lowercased. Raises HTTPException(404) if it isn't one."""
    if not is_mbid(mbid):
        raise HTTPException(404, "not an artist MBID")
    return mbid.strip().lower()


def _wishlist_item(form: dict[str, str]) -> WishlistItem:
    """The wishlist item a result's form describes. Raises HTTPException(400) for a missing
    title or a malformed MBID."""
    mbid = form.get("release_group", "").strip()
    release = form.get("release", "").strip()
    artists = tuple(a for a in form.get("artist_mbids", "").split(",") if a.strip())
    names = _artist_names(form.get("artist_names", ""), len(artists))
    title = clean(form.get("title", ""))
    if not is_mbid(mbid) or (release and not is_mbid(release)) or not title:
        raise HTTPException(400, "not a release group from the search results")
    if not all(is_mbid(a) for a in artists):
        raise HTTPException(400, "malformed artist MBID")
    return WishlistItem(
        release_group_mbid=mbid.lower(),
        release_mbid=release.lower() or None,
        title=title,
        artist_credit=clean(form.get("artist_credit", "")),
        artist_mbids=tuple(a.strip().lower() for a in artists),
        artist_names=names,
        primary_type=clean(form.get("primary_type", "")),
        secondary_types=tuple(t for t in clean(form.get("secondary_types", "")).split(",") if t),
        first_release_date=clean(form.get("first_release_date", ""))[:10],
    )


def _artist_names(value: str, count: int) -> tuple[str, ...]:
    """The credited artists' names from a result's form (a JSON list, since a name can have a
    comma in it); () unless it's one name per artist MBID."""
    try:
        names = json.loads(value) if value else []
    except ValueError:
        return ()
    if not isinstance(names, list) or len(names) != count:
        return ()
    return tuple(clean(n) if isinstance(n, str) else "" for n in names)


def _multiline(text: str) -> str:
    """A textarea's text with its line breaks kept (browsers send CRLF), trimmed."""
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def _local_path(path: str) -> str:
    """`path` if it's a path on this site (root-relative, so it works behind a proxy), else
    "/": a form's `next` must not send the browser elsewhere."""
    return path if path.startswith("/") and not path.startswith(("//", "/\\")) else "/"


def _contact_problem(settings: Settings) -> str | None:
    """Why the app can't call MusicBrainz / ListenBrainz yet, if it can't."""
    try:
        validate_contact(settings.contact)
    except ValueError as exc:
        return str(exc)
    return None
