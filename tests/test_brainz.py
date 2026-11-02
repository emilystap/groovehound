import urllib.parse

import pytest

from groovehound.brainz import listenbrainz, musicbrainz
from groovehound.brainz.http import RateLimiter
from groovehound.config import Settings

from .conftest import CONTACT, FakeClock, FakeOpener

ARTIST = "260b6184-8828-48eb-945c-bc4cb6fc34ca"


def params(request) -> dict[str, list[str]]:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)


def mb_client(opener: FakeOpener) -> musicbrainz.MusicBrainzClient:
    clock = FakeClock()
    return musicbrainz.MusicBrainzClient(
        limiter=RateLimiter(clock=clock, sleep=clock.sleep), contact=CONTACT, opener=opener
    )


def test_client_for_requires_a_contact():
    with pytest.raises(ValueError, match="contact"):
        musicbrainz.client_for(Settings())
    with pytest.raises(ValueError, match="contact"):
        listenbrainz.client_for(Settings())


def test_public_server_gets_at_least_the_minimum_interval():
    with pytest.raises(ValueError, match=r"1\.5"):
        musicbrainz.client_for(Settings(contact=CONTACT, musicbrainz_interval=1.0))
    assert musicbrainz.client_for(Settings(contact=CONTACT)).limiter is musicbrainz.LIMITER


def test_mirror_may_go_faster():
    client = musicbrainz.client_for(
        Settings(contact=CONTACT, musicbrainz_api_root="http://mirror:5000", musicbrainz_interval=0)
    )
    assert client.api_root == "http://mirror:5000/ws/2"
    assert client.limiter.interval == 0


def test_search_query_quotes_every_field():
    assert (
        musicbrainz.search_query(releasegroup='A "B" \\ C', artist="X", other="")
        == 'releasegroup:"A \\"B\\" \\\\ C" AND artist:"X"'
    )
    with pytest.raises(ValueError):
        musicbrainz.search_query(artist="")


def test_search_is_one_request():
    opener = FakeOpener({"count": 0, "release-groups": []})
    mb_client(opener).search_release_groups("Brat", "Charli xcx")
    (request,) = opener.requests
    assert urllib.parse.urlsplit(request.full_url).path == "/ws/2/release-group"
    assert params(request)["query"] == ['releasegroup:"Brat" AND artist:"Charli xcx"']
    assert params(request)["fmt"] == ["json"]


def test_artist_release_groups_pages_and_stops():
    page = [{"id": str(i)} for i in range(100)]
    opener = FakeOpener(
        {"release-group-count": 1000, "release-groups": page},
        {"release-group-count": 1000, "release-groups": page},
        {"release-group-count": 1000, "release-groups": page},
    )
    data = mb_client(opener).artist_release_groups(ARTIST.upper())
    assert len(opener.requests) == musicbrainz.MAX_BROWSE_PAGES
    assert len(data["release-groups"]) == 300
    assert data["release-group-count"] == 1000
    first, last = params(opener.requests[0]), params(opener.requests[-1])
    assert first["artist"] == [ARTIST]
    assert first["type"] == ["album|ep"]
    assert first["limit"] == ["100"]
    assert first["release-group-status"] == ["website-default"]
    assert last["offset"] == ["200"]


def test_artist_release_groups_stops_when_complete():
    opener = FakeOpener({"release-group-count": 2, "release-groups": [{"id": "a"}, {"id": "b"}]})
    assert mb_client(opener).artist_release_groups(ARTIST)["release-group-count"] == 2
    assert len(opener.requests) == 1


def test_lookups_validate_mbids():
    with pytest.raises(ValueError):
        mb_client(FakeOpener()).release("not-an-mbid")


def test_release_lookup_asks_for_purchase_links():
    opener = FakeOpener({"id": ARTIST})
    mb_client(opener).release(ARTIST)
    assert "url-rels" in params(opener.requests[0])["inc"][0].split()


def lb_client(opener: FakeOpener, token: str = "") -> listenbrainz.ListenBrainzClient:
    clock = FakeClock()
    return listenbrainz.ListenBrainzClient(
        limiter=RateLimiter(clock=clock, sleep=clock.sleep),
        contact=CONTACT,
        opener=opener,
        token=token,
    )


def test_listenbrainz_token_only_when_configured():
    opener = FakeOpener({}, {})
    lb_client(opener).fresh_releases()
    lb_client(opener, token="secret").fresh_releases()
    assert opener.requests[0].get_header("Authorization") is None
    assert opener.requests[1].get_header("Authorization") == "Token secret"
    assert "secret" not in repr(lb_client(opener, token="secret"))


def test_fresh_releases_window_is_capped():
    opener = FakeOpener({})
    lb_client(opener).fresh_releases(days=365)
    assert params(opener.requests[0])["days"] == ["90"]


def test_user_names_are_escaped():
    opener = FakeOpener({})
    lb_client(opener).user_fresh_releases("a/b c")
    assert "/1/user/a%2Fb%20c/fresh_releases" in opener.requests[0].full_url
