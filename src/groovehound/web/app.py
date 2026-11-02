"""FastAPI app: server-rendered Jinja templates, HTMX (vendored in static/) for partial updates.

Each request opens its own Store. Routes that act for a user get it from `current_user`, the
only place that decides who that is: today always the owner (`db.OWNER_ID`); with accounts,
the logged-in user from the session.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..brainz import listenbrainz, musicbrainz
from ..brainz.http import validate_contact
from ..config import Settings
from ..db import OWNER_ID, Store, User

HERE = Path(__file__).parent


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

    app = FastAPI(title="Groovehound", version=__version__, docs_url=None, redoc_url=None)
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
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz() -> str:
        return "ok"

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, user: UserDep) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "home.html",
            {"page": "home", "user": user, "contact_problem": _contact_problem(settings)},
        )

    return app


def _contact_problem(settings: Settings) -> str | None:
    """Why the app can't call MusicBrainz / ListenBrainz yet, if it can't."""
    try:
        validate_contact(settings.contact)
    except ValueError as exc:
        return str(exc)
    return None
