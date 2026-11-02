import pytest

from groovehound import __version__
from groovehound.brainz.http import (
    JsonClient,
    NotFound,
    RateLimiter,
    ServiceError,
    ServicePause,
    requested_wait,
    user_agent,
)

from .conftest import CONTACT, FakeClock, FakeOpener


def make_client(opener: FakeOpener, clock: FakeClock, **kwargs) -> JsonClient:
    limiter = RateLimiter(1.5, clock=clock, sleep=clock.sleep)
    return JsonClient(
        api_root="https://example.org", limiter=limiter, contact=CONTACT, opener=opener, **kwargs
    )


def test_user_agent_names_app_version_and_contact():
    assert user_agent(CONTACT) == f"Groovehound/{__version__} ( {CONTACT} )"


@pytest.mark.parametrize("contact", ["", "   ", "a (b)", "line\nbreak", "x" * 201])
def test_contact_is_required_and_header_safe(contact):
    with pytest.raises(ValueError):
        user_agent(contact)


def test_requests_are_spaced_from_each_response():
    clock = FakeClock()
    client = make_client(FakeOpener({}, {}, {}), clock)
    for _ in range(3):
        client.get_json("/x")
    assert clock.slept == [1.5, 1.5]
    assert client.requests == 3


def test_headers_and_gzip():
    clock = FakeClock()
    opener = FakeOpener({"ok": True}, gzipped=True)
    client = make_client(opener, clock, headers={"Authorization": "Token t"})
    assert client.get_json("/x", {"a": 1, "skip": None}) == {"ok": True}
    request = opener.requests[0]
    assert request.full_url == "https://example.org/x?a=1"
    assert request.get_header("User-agent").startswith("Groovehound/")
    assert request.get_header("Accept-encoding") == "gzip"
    assert request.get_header("Authorization") == "Token t"


def test_404_is_not_found():
    with pytest.raises(NotFound):
        make_client(FakeOpener(404), FakeClock()).get_json("/x")


def test_503_backs_off_then_succeeds():
    clock = FakeClock()
    client = make_client(FakeOpener(503, {"ok": 1}), clock)
    assert client.get_json("/x") == {"ok": 1}
    assert clock.slept == [15.0]


def test_429_waits_as_long_as_listenbrainz_asks():
    clock = FakeClock()
    client = make_client(FakeOpener((429, {"X-RateLimit-Reset-In": "40"}), {"ok": 1}), clock)
    client.get_json("/x")
    assert clock.slept == [40.0]


def test_gives_up_after_backoff():
    clock = FakeClock()
    client = make_client(FakeOpener(503, 503, 503), clock)
    with pytest.raises(ServiceError):
        client.get_json("/x")
    assert clock.slept == [15.0, 60.0]


def test_long_retry_after_gives_up_at_once_and_holds_the_limiter():
    clock = FakeClock()
    client = make_client(FakeOpener((503, {"Retry-After": "3600"})), clock)
    with pytest.raises(ServiceError) as caught:
        client.get_json("/x")
    assert caught.value.retry_after == 3600
    assert clock.slept == []
    client.limiter.wait()
    assert clock.slept == [pytest.approx(3600)]


def test_spent_window_holds_the_next_request():
    clock = FakeClock()
    opener = FakeOpener(({}, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-In": "20"}), {})
    client = make_client(opener, clock)
    client.get_json("/x")
    client.get_json("/x")
    assert clock.slept == [20.0]


def test_server_errors_stop():
    with pytest.raises(ServiceError):
        make_client(FakeOpener(500), FakeClock()).get_json("/x")


def test_requested_wait():
    assert requested_wait({"Retry-After": "5", "X-RateLimit-Reset-In": "9"}) == 9
    assert requested_wait({"Retry-After": "soon"}) is None
    assert requested_wait({}) is None


def test_service_pause_lasts_an_hour_or_as_asked():
    clock = FakeClock()
    pause = ServicePause(clock=clock)
    assert pause.remaining() == 0
    pause.start()
    clock.now += 600
    assert pause.remaining() == 3000
    pause.start(retry_after=7200)
    assert pause.remaining() == 7200
    pause.clear()
    assert pause.remaining() == 0
