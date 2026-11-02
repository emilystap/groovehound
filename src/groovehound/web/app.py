"""FastAPI app: server-rendered Jinja templates, HTMX (vendored in static/) for partial updates.

Each request opens its own Store. Routes that act for a user get it from `current_user`, the
only place that decides who that is: today always the owner (`db.OWNER_ID`); with accounts,
the logged-in user from the session. While the app runs, `reconcile.Reconciler` re-browses
followed artists in the background (roadmap 2c).
"""

from __future__ import annotations

import json
import mimetypes
from collections.abc import AsyncIterator, Iterator
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
    buy,
    covers,
    follows,
    new_releases,
    personal,
    playlists,
    releases,
    similar,
)
from ..brainz import coverart, listenbrainz, musicbrainz
from ..brainz.http import NotFound, ServiceError, validate_contact
from ..brainz.musicbrainz import is_mbid
from ..config import Settings
from ..db import OWNER_ID, Store, User, WishlistItem, local_date, utcnow
from ..reconcile import Reconciler
from ..search import KINDS, clean, search
from ..share import parse_share

HOME_WISHLIST = 10  # most recent items on the home page
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
    interval below the limit for musicbrainz.org)."""
    if settings.contact:
        musicbrainz.client_for(settings)  # validates contact, api_root and interval at startup
    buy.check_qobuz_store(settings.qobuz_store)

    # The slow re-browse of followed artists (roadmap 2c), only once MusicBrainz can be asked
    reconciler = None if _contact_problem(settings) else Reconciler(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if reconciler:
            reconciler.start()
        try:
            yield
        finally:
            if reconciler:
                reconciler.shutdown()

    app = FastAPI(
        title="Groovehound", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan
    )
    app.state.settings = settings
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
    templates.env.globals["followable"] = follows.credited  # a credit's *Follow* buttons
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
        feed = new_releases.feed(store, user.id)
        lb_user = _listenbrainz_user(user)
        suggested = personal.suggestions(store, user.id, lb_user)
        hits = [r.hit for r in (*feed.upcoming, *feed.new)] + suggested
        return templates.TemplateResponse(
            request,
            "home.html",
            {
                "page": "home",
                "user": user,
                "contact_problem": _contact_problem(settings),
                "wishlist": store.wishlist(user.id, limit=HOME_WISHLIST),
                "wishlist_count": store.wishlist_count(user.id),
                "captures": store.captures(user.id),
                "follow_count": len(store.follows(user.id)),
                "feed": feed,
                "listenbrainz_user": lb_user,
                "suggested": suggested,
                "today": local_date(utcnow()),  # upcoming suggestions say "out <date>"
                "followed": store.followed(user.id, (a for h in suggested for a in h.artist_mbids)),
                "saved_ids": store.wishlisted(user.id, (h.mbid for h in hits)),
                "draft": draft,
                "here": "/",
            },
        )

    @app.get("/discover", response_class=HTMLResponse)
    def discover(request: Request, store: StoreDep, user: UserDep) -> HTMLResponse:
        """The playlists ListenBrainz created for you, as albums (no requests)."""
        lb_user = _listenbrainz_user(user)
        found = playlists.playlists(store, user.id, lb_user)
        hits = [h for p in found for h in p.releases]
        return templates.TemplateResponse(
            request,
            "discover.html",
            {
                "page": "discover",
                "user": user,
                "listenbrainz_user": lb_user,
                "playlists": found,
                "followed": store.followed(user.id, (a for h in hits for a in h.artist_mbids)),
                "saved_ids": store.wishlisted(user.id, (h.mbid for h in hits)),
                "here": "/discover",
            },
        )

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
        """A cover thumbnail from the cover cache (one request to the Cover Art Archive the first
        time, or when due for a refresh). Only for release groups on your wishlist, by artists
        you follow, in your ListenBrainz fresh releases (the home page) or in the playlists it
        created for you (Discover), so this isn't a way to make the server fetch anything
        else."""
        mbid = mbid.lower()
        if not is_mbid(mbid) or not (
            store.wishlisted(user.id, [mbid])
            or store.followed_release_group(user.id, mbid)
            or personal.listed(store, _listenbrainz_user(user), mbid)
            or playlists.listed(store, _listenbrainz_user(user), mbid)
        ):
            raise HTTPException(404, "not on your wishlist, home page or Discover")
        client = None if _contact_problem(settings) else coverart.client_for(settings)
        try:
            found = covers.cover(store, client, coverart.PAUSE, mbid)
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
        """Everything still to buy, newest first; `?show=bought` lists the bought items."""
        bought = show == "bought"
        items = store.wishlist(user.id, bought=bought)
        return templates.TemplateResponse(
            request,
            "wishlist.html",
            {
                "page": "wishlist",
                "bought": bought,
                "items": items,
                "captures": [] if bought else store.captures(user.id),
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
        """Save an item's notes and "where I heard it". HTMX gets the updated item back."""
        form = await _form(request)
        notes = _multiline(form.get("notes", ""))[:MAX_NOTES]
        if not store.annotate_wishlist_item(user.id, item_id, notes, clean(form.get("heard", ""))):
            raise HTTPException(404, "not on your wishlist")
        if request.headers.get("HX-Request"):
            item = store.wishlist_item(user.id, item_id)
            return templates.TemplateResponse(
                request,
                "_wishlist_item.html",
                {"item": item, **item_context(store, user, [item]), "here": form.get("next", "")},
            )
        return RedirectResponse(_local_path(form.get("next", "/wishlist")), status_code=303)

    @app.get("/wishlist/{item_id}/editions")
    def wishlist_item_editions(
        request: Request, item_id: int, store: StoreDep, user: UserDep, next: str = ""
    ) -> Response:
        """Every edition of an item's album, asked for when its *Choose edition* is opened: one
        request unless cached (the same browse as *Where to buy*). HTMX gets the list in place;
        without JavaScript, back to the item on the list."""
        item = store.wishlist_item(user.id, item_id)
        if item is None:
            raise HTTPException(404, "not on your wishlist")
        error = fetch_releases(store, item)
        if request.headers.get("HX-Request"):
            return templates.TemplateResponse(
                request,
                "_editions.html",
                {
                    "item": item,
                    "choice": releases.choice(store, item),
                    "error": error,
                    "here": next,
                },
            )
        back = _local_path(next or "/wishlist")
        return RedirectResponse(f"{back}#wish-{item.id}", status_code=303)

    @app.post("/wishlist/{item_id}/edition")
    async def choose_edition(
        request: Request, item_id: int, store: StoreDep, user: UserDep
    ) -> Response:
        """Save which edition an item is for (`release`, empty = any edition). HTMX gets the
        updated item back, with that edition's links first under *Where to buy*."""
        form = await _form(request)
        release = form.get("release", "").strip()
        if release and not is_mbid(release):
            raise HTTPException(400, "malformed release MBID")
        if not store.choose_edition(user.id, item_id, release.lower() or None):
            raise HTTPException(404, "not on your wishlist")
        if request.headers.get("HX-Request"):
            item = store.wishlist_item(user.id, item_id)
            return templates.TemplateResponse(
                request,
                "_wishlist_item.html",
                {"item": item, **item_context(store, user, [item]), "here": form.get("next", "")},
            )
        back = _local_path(form.get("next", "/wishlist"))
        return RedirectResponse(f"{back}#wish-{item_id}", status_code=303)

    @app.get("/wishlist/{item_id}/links")
    def wishlist_item_links(
        request: Request, item_id: int, store: StoreDep, user: UserDep, next: str = ""
    ) -> Response:
        """MusicBrainz's purchase and streaming links for an item, asked for when its *Where to
        buy* is opened: one request unless they're cached. HTMX gets them in place; without
        JavaScript, back to the item on the list."""
        item = store.wishlist_item(user.id, item_id)
        if item is None:
            raise HTTPException(404, "not on your wishlist")
        error = fetch_releases(store, item)
        if request.headers.get("HX-Request"):
            return templates.TemplateResponse(
                request,
                "_buy_links.html",
                {
                    "item": item,
                    "where": buy.where_to_buy(store, item, settings.qobuz_store),
                    "error": error,
                    "here": next,
                },
            )
        back = _local_path(next or "/wishlist")
        return RedirectResponse(f"{back}#wish-{item.id}", status_code=303)

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
        return templates.TemplateResponse(
            request,
            "artists.html",
            {"page": "artists", "follows": store.follows(user.id), "here": "/artists"},
        )

    @app.get("/artists/{mbid}", response_class=HTMLResponse)
    def artist_page(request: Request, mbid: str, store: StoreDep, user: UserDep) -> HTMLResponse:
        """An artist's albums and EPs, with *Follow*: one browse unless cached (the same one
        following makes, so following from here asks nothing more)."""
        mbid = _artist_mbid(mbid)
        contact_problem = _contact_problem(settings)
        follow = store.follow_of(user.id, mbid)
        found, error = None, None
        if contact_problem:
            found, _ = follows.cached_discography(store, mbid)
        else:
            client = musicbrainz.client_for(settings)
            try:
                found = follows.discography(store, client, musicbrainz.PAUSE, mbid)
                if follow and "single" in follow.types:  # followed for singles: keep them fresh
                    found = replace(
                        found, singles=follows.singles(store, client, musicbrainz.PAUSE, mbid)
                    )
            except NotFound as exc:
                raise HTTPException(404, "no such artist on MusicBrainz") from exc
            except ServiceError as exc:
                found, _ = follows.cached_discography(store, mbid)
                error = str(exc)
        hits = (*found.release_groups, *(found.singles or ())) if found else ()
        return templates.TemplateResponse(
            request,
            "artist.html",
            {
                "page": "artists",
                "mbid": mbid,
                "found": found,
                # what to call them when MusicBrainz couldn't be asked: the name as followed
                "name": found.artist.name if found else follow.name if follow else "",
                "following": follow is not None,
                "follow": follow,
                "error": error,
                "contact_problem": contact_problem,
                "saved_ids": store.wishlisted(user.id, (h.mbid for h in hits)),
                **similar_context(store, user, mbid),
                "here": f"/artists/{mbid}",
            },
        )

    def similar_context(
        store: Store, user: User, mbid: str, error: str | None = None
    ) -> dict[str, object]:
        """What the artist page's *Similar artists* card shows (no requests)."""
        found = similar.cached(store, user.id, mbid)
        return {
            "similar": found,
            "similar_error": error,
            "followed": store.followed(user.id, (a.mbid for a in (found.artists if found else ()))),
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
        """Follow an artist: the first time, one browse of their albums and EPs (unless cached),
        so what's already out isn't reported as new. HTMX gets every *Follow* button for them
        on the page updated; a plain form post goes back to `next`."""
        mbid = _artist_mbid(mbid)
        form = await _form(request)
        error = _contact_problem(settings)
        if not error:
            try:
                follows.follow(
                    store, musicbrainz.client_for(settings), musicbrainz.PAUSE, user.id, mbid
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
            {"mbid": mbid, "follow": follow, "error": error, "saved": not error, "here": here},
            headers=headers,
        )

    @app.post("/captures")
    async def add_capture(request: Request, store: StoreDep, user: UserDep) -> Response:
        """Save free text to look up later. HTMX gets the quick capture card back (an empty
        form and the updated list); a plain form post goes back to `next`."""
        form = await _form(request)
        text = " ".join(form.get("text", "").split())[:MAX_CAPTURE]
        if not text:
            raise HTTPException(400, "nothing to save")
        store.add_capture(user.id, text)
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
                {"captures": store.captures(user.id), "here": form.get("next", "")},
            )
        return RedirectResponse(_local_path(form.get("next", "/")), status_code=303)

    def item_context(store: Store, user: User, items: list[WishlistItem]) -> dict[str, object]:
        """What wishlist items show without a request: *Where to buy* and *Choose edition* by
        item id, and which of their artists are followed."""
        return {
            "where_to_buy": {i.id: buy.where_to_buy(store, i, settings.qobuz_store) for i in items},
            "editions": {i.id: releases.choice(store, i) for i in items},
            "followed": store.followed(user.id, (a for i in items for a in i.artist_mbids)),
        }

    def fetch_releases(store: Store, item: WishlistItem) -> str | None:
        """Make sure the item's album's editions are cached (one request if they aren't, or are
        due); why not, if that failed."""
        error = _contact_problem(settings)
        if not error:
            try:
                releases.fetch(
                    store,
                    musicbrainz.client_for(settings),
                    musicbrainz.PAUSE,
                    item.release_group_mbid,
                )
            except ServiceError as exc:
                error = str(exc)
        return error

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
                    "bought": parse_qs(urlsplit(form.get("next", "")).query).get("show")
                    == ["bought"],
                    **_wishlist_counts(store, user),
                },
            )
        return RedirectResponse(_local_path(form.get("next", "/wishlist")), status_code=303)

    return app


def _wishlist_counts(store: Store, user: User) -> dict[str, int]:
    return {
        "wanted_count": store.wishlist_count(user.id),
        "bought_count": store.wishlist_count(user.id, bought=True),
    }


async def _form(request: Request) -> dict[str, str]:
    """A urlencoded form's fields (first value each). Parsed by hand, as in tagdrifter, to
    avoid the python-multipart dependency for a few small forms."""
    return {k: v[0] for k, v in (await _form_lists(request)).items()}


async def _form_lists(request: Request) -> dict[str, list[str]]:
    """A urlencoded form's fields with every value (checkboxes sharing a name)."""
    return parse_qs((await request.body()).decode(errors="replace"))


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
