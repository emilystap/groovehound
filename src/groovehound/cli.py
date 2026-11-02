"""Command line interface: `groovehound serve | config`."""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .config import load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="groovehound", description=__doc__)
    parser.add_argument("--version", action="version", version=f"groovehound {__version__}")
    parser.add_argument("--config", type=Path, help="path to config.toml")
    parser.add_argument("--db", type=Path, help="SQLite database path")
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="run the web UI")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)

    commands.add_parser("config", help="print the settings in effect (secrets hidden)")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        settings = load_settings(args.config, database_path=args.db)
    except (OSError, ValueError) as exc:
        print(f"groovehound: {exc}", file=sys.stderr)
        return 2

    if args.command == "config":
        shown = asdict(settings)
        shown["listenbrainz_token"] = "(set)" if settings.listenbrainz_token else ""
        for key, value in shown.items():
            print(f"{key} = {value}")
        return 0

    import uvicorn

    from .web import create_app

    try:
        app = create_app(settings)
    except ValueError as exc:
        print(f"groovehound: {exc}", file=sys.stderr)
        return 2
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=True)
    return 0
