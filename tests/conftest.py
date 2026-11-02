from __future__ import annotations

import gzip
import io
import json
import urllib.error
from email.message import Message
from pathlib import Path

import pytest

from groovehound.brainz import http
from groovehound.config import Settings

CONTACT = "tests@example.com"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never call MusicBrainz or ListenBrainz; pass a FakeOpener to clients instead."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("tests must not make real HTTP requests")

    monkeypatch.setattr(http, "_urlopen", refuse)


def settings_for(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(database_path=tmp_path / "groovehound.db", contact=CONTACT, **overrides)


class FakeClock:
    """A monotonic clock that only moves when something sleeps on it."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class FakeResponse:
    def __init__(self, body: bytes, headers: dict[str, str]) -> None:
        self._body = body
        self.headers = Message()
        for k, v in headers.items():
            self.headers[k] = v

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        pass


def _headers(values: dict[str, str]) -> Message:
    message = Message()
    for k, v in values.items():
        message[k] = v
    return message


class FakeOpener:
    """Answers queued responses in order and records each request. Queue a dict (JSON body),
    an int (HTTP error status) or (status, headers)."""

    def __init__(self, *answers: object, gzipped: bool = False) -> None:
        self.answers = list(answers)
        self.gzipped = gzipped
        self.requests: list = []

    def __call__(self, request, timeout: float = 0) -> FakeResponse:
        self.requests.append(request)
        answer = self.answers.pop(0)
        headers: dict[str, str] = {}
        if isinstance(answer, tuple):
            answer, headers = answer
        if isinstance(answer, int):
            raise urllib.error.HTTPError(
                request.full_url, answer, "error", _headers(headers), io.BytesIO(b"")
            )
        body = json.dumps(answer).encode()
        if self.gzipped:
            body = gzip.compress(body)
            headers = {**headers, "Content-Encoding": "gzip"}
        return FakeResponse(body, headers)
