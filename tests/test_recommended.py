from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from groovehound import recommended
from groovehound.brainz.http import ServiceError, ServicePause
from groovehound.db import OWNER_ID, Follow, LibraryRelease, Store, WishlistItem
from groovehound.web import create_app

from .conftest import FakeOpener, settings_for
from .test_brainz import lb_client, params
from .test_follows import store_at

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
USER = "Rob"
UPDATED = 1792709778  # 2026-10-22 22:56 UTC
HOPKINS = "0b0c25f4-f31c-46a5-a4fb-ccbf53d663bd"
ROYKSOPP = "1c70a3fc-fa3c-4be1-8b55-c3192db8a884"
GOLDFRAPP = "5e00c39c-9c65-420f-aa26-d68f414a74c6"
REC = [f"4b8ac19a-3333-4261-9f81-fe5efd5383{i:02x}" for i in range(8)]
RG_SINGULARITY = "0a663aab-76e3-4bf8-8b73-39f5c076bc01"
RG_NEBULOUS = "0a663aab-76e3-4bf8-8b73-39f5c076bc02"
RG_IMMUNITY = "0a663aab-76e3-4bf8-8b73-39f5c076bc03"
RG_OWNED = "0a663aab-76e3-4bf8-8b73-39f5c076bc04"
ROOT = "https://api.listenbrainz.org/1"


def rec(recording: str, score: float, listened: str | None = None) -> dict:
    """One recommendation, as ListenBrainz gives it (checked 2026-11-02)."""
    return {"latest_listened_at": listened, "recording_mbid": recording, "score": score}


def recommendations(*mbids: dict, updated: int = UPDATED) -> dict:
    return {
        "payload": {
            "count": len(mbids),
            "entity": "recording",
            "last_updated": updated,
            "mbids": list(mbids),
            "offset": 0,
            "total_mbid_count": 1000,
            "user_name": "rob",
        }
    }


RECOMMENDATIONS = recommendations(
    rec(REC[0], 1.02, "2026-09-30T19:43:24.000Z"),  # played: left out
    rec(REC[1], 0.94),
    rec(REC[2], 0.90),
    rec(REC[3], 0.85),
    rec(REC[4], 0.97),  # the best unplayed, though listed later
    rec(REC[5], 0.80),
    rec(REC[6], 0.70),  # no release group known
)


def meta(song: str, group: str, album: str, credit: str, *artists: tuple[str, str]) -> dict:
    """One recording's metadata with `inc=artist release` (checked 2026-11-02, trimmed)."""
    return {
        "artist": {
            "artists": [
                {"artist_mbid": m, "join_phrase": "", "name": n, "type": "Person"}
                for m, n in artists
            ],
            "name": credit,
        },
        "recording": {"length": 160000, "name": song},
        "release": {
            "album_artist_name": credit,
            "mbid": f"{group[:-4]}ffff",
            "name": album,
            "release_group_mbid": group,
            "year": 2026,
        },
    }


METADATA = {
    REC[1]: meta("Emerald Rush", RG_SINGULARITY, "Singularity", "Jon Hopkins",
                 (HOPKINS, "Jon Hopkins")),
    REC[2]: meta("We Enter", RG_NEBULOUS, "Nebulous Nights", "Röyksopp",
                 (ROYKSOPP, "Röyksopp"), (GOLDFRAPP, "Alison Goldfrapp")),
    REC[3]: meta("Luminous Beings", RG_SINGULARITY, "Singularity", "Jon Hopkins",
                 (HOPKINS, "Jon Hopkins")),
    REC[4]: meta("Open Eye Signal", RG_IMMUNITY, "Immunity", "Jon Hopkins",
                 (HOPKINS, "Jon Hopkins")),
    REC[5]: meta("Owned", RG_OWNED, "Already Mine", "Jon Hopkins", (HOPKINS, "Jon Hopkins")),
    # REC[6]: not in the metadata
}  # fmt: skip


def fetch(store: Store, opener: FakeOpener, pause: ServicePause | None = None, now=NOW, user=USER):
    return recommended.fetch(store, lb_client(opener), pause or ServicePause(), user, now=now)


def urls(opener: FakeOpener) -> list[str]:
    return [r.full_url.split("?")[0] for r in opener.requests]


def test_the_recommendations_daily_and_their_unplayed_recordings(tmp_path: Path):
    opener = FakeOpener(RECOMMENDATIONS, METADATA, RECOMMENDATIONS)
    with store_at(tmp_path) as store:
        assert fetch(store, opener) == 2
        assert urls(opener) == [
            f"{ROOT}/cf/recommendation/user/Rob/recording",
            f"{ROOT}/metadata/recording/",
        ]
        assert params(opener.requests[0]) == {"count": ["100"]}
        assert params(opener.requests[1]) == {
            "recording_mbids": [",".join(REC[1:7])],  # not the played one
            "inc": ["artist release"],
        }
        assert fetch(store, opener, now=NOW + timedelta(hours=21)) is None  # nothing due
        # the same model: the metadata is known already
        assert fetch(store, opener, now=NOW + timedelta(hours=27)) == 1
    assert len(opener.requests) == 3


def test_a_new_model_fetches_the_metadata_again(tmp_path: Path):
    newer = recommendations(rec(REC[1], 0.9), updated=UPDATED + 7 * 86400)
    opener = FakeOpener(RECOMMENDATIONS, METADATA, newer, {REC[1]: METADATA[REC[1]]})
    with store_at(tmp_path) as store:
        fetch(store, opener)
        assert fetch(store, opener, now=NOW + timedelta(hours=27)) == 2
        assert params(opener.requests[3])["recording_mbids"] == [REC[1]]


def test_the_metadata_in_batches_of_fifty(tmp_path: Path):
    many = [f"4b8ac19a-3333-4261-9f81-fe5efd53{i:04x}" for i in range(60)]
    opener = FakeOpener(recommendations(*(rec(m, 0.5) for m in many)), {}, {})
    with store_at(tmp_path) as store:
        assert fetch(store, opener) == 3
    assert [len(params(r)["recording_mbids"][0].split(",")) for r in opener.requests[1:]] == [
        50,
        10,
    ]


def test_a_bad_gateway_waits_for_the_next_day_without_a_pause(tmp_path: Path):
    many = [f"4b8ac19a-3333-4261-9f81-fe5efd53{i:04x}" for i in range(60)]
    first = {m: METADATA[REC[1]] for m in many[:50]}
    data = recommendations(*(rec(m, 0.5) for m in many))
    opener = FakeOpener(data, first, 502, data, {m: METADATA[REC[1]] for m in many[50:]}, 502)
    pause = ServicePause()
    with store_at(tmp_path) as store:
        assert fetch(store, opener, pause) == 3  # the second batch failed
        assert not pause.remaining()
        found = recommended.recommended(store, OWNER_ID, USER)
        assert found.unmatched == 10
        later = NOW + timedelta(hours=27)
        assert fetch(store, opener, pause, now=later) == 2  # only the 10 left
        assert recommended.recommended(store, OWNER_ID, USER).unmatched == 0
        # the recommendations themselves: what's cached stays, asked again the next day
        assert fetch(store, opener, pause, now=later + timedelta(hours=27)) == 1
        assert not pause.remaining()
        assert recommended.recommended(store, OWNER_ID, USER).unmatched == 0
        assert fetch(store, opener, pause, now=later + timedelta(hours=28)) is None


def test_other_errors_pause_listenbrainz(tmp_path: Path):
    pause = ServicePause()
    with store_at(tmp_path) as store:
        with pytest.raises(ServiceError):
            fetch(store, FakeOpener(500), pause)
        assert pause.remaining()
    pause = ServicePause()
    with store_at(tmp_path) as store:
        with pytest.raises(ServiceError):
            fetch(store, FakeOpener(RECOMMENDATIONS, 500), pause, now=NOW + timedelta(days=2))
        assert pause.remaining()  # the metadata too


def test_albums_best_first_with_their_songs(tmp_path: Path):
    with store_at(tmp_path) as store:
        fetch(store, FakeOpener(RECOMMENDATIONS, METADATA))
        found = recommended.recommended(store, OWNER_ID, USER)
        assert [h.mbid for h in found.albums] == [
            RG_IMMUNITY,  # 0.97
            RG_SINGULARITY,  # 0.94, with a song of 0.85
            RG_NEBULOUS,
            RG_OWNED,
        ]
        singularity = found.albums[1]
        assert singularity.title == "Singularity"
        assert singularity.songs == ("Emerald Rush", "Luminous Beings")
        nebulous = found.albums[2]
        assert nebulous.artist_credit == "Röyksopp"
        assert nebulous.artist_mbids == (ROYKSOPP, GOLDFRAPP)
        assert nebulous.artist_names == ("Röyksopp", "Alison Goldfrapp")
        assert found.unmatched == 1
        assert found.left_out == 0
        assert found.last_updated == "2026-10-22T22:56:18+00:00"


def test_owned_played_saved_and_dismissed_albums_are_left_out(tmp_path: Path):
    with store_at(tmp_path) as store:
        fetch(store, FakeOpener(RECOMMENDATIONS, METADATA))
        owned = LibraryRelease(f"{RG_OWNED[:-4]}ffff", RG_OWNED, (HOPKINS,), "Already Mine",
                               "Jon Hopkins", "Jon Hopkins/Already Mine", 9)  # fmt: skip
        store.replace_library(OWNER_ID, [owned], "2026-10-01T00:00:00+00:00")
        store.replace_listened(OWNER_ID, {RG_IMMUNITY: 3})
        store.add_to_wishlist(OWNER_ID, WishlistItem(RG_NEBULOUS, "Nebulous Nights", "Röyksopp"))
        store.dismiss(OWNER_ID, "release-group", RG_SINGULARITY)  # shared with the home page
        found = recommended.recommended(store, OWNER_ID, USER)
        assert found.albums == ()
        assert found.left_out == 4
        assert recommended.listed(store, USER, RG_SINGULARITY)  # still recommended: its cover
        assert not recommended.listed(store, USER, "0a663aab-76e3-4bf8-8b73-39f5c076bcff")


def test_none_and_unknown_users(tmp_path: Path):
    opener = FakeOpener(b"", 404)
    pause = ServicePause()
    with store_at(tmp_path) as store:
        assert fetch(store, opener, user=" ") is None
        assert recommended.recommended(store, OWNER_ID, "") is None
        assert fetch(store, opener, pause) == 1  # a 204: none
        assert recommended.recommended(store, OWNER_ID, USER).albums == ()
        assert fetch(store, opener, pause, now=NOW + timedelta(hours=27)) == 1  # a 404
        assert not pause.remaining()
        assert recommended.recommended(store, OWNER_ID, USER) is None
    assert len(opener.requests) == 2


def test_the_discover_page(tmp_path: Path):
    settings = settings_for(tmp_path, listenbrainz_user=USER)
    with Store(settings.database_path) as store:
        fetch(store, FakeOpener(RECOMMENDATIONS, METADATA))
        store.follow(OWNER_ID, Follow(HOPKINS, "Jon Hopkins"))
        store.dismiss(OWNER_ID, "release-group", RG_OWNED)
        store.save_cover(RG_NEBULOUS, b"jpeg", "image/jpeg", "2099-01-01T00:00:00+00:00")
    client = TestClient(create_app(settings))
    page = client.get("/discover").text
    assert "Recommended for you" in page
    assert page.index("Immunity") < page.index("Singularity")
    assert "“Emerald Rush”, “Luminous Beings”" in page
    assert f'action="/artists/{GOLDFRAPP}/follow"' in page
    assert f'action="/artists/{HOPKINS}/unfollow"' in page  # followed already
    assert f'name="release_group" value="{RG_NEBULOUS}"' in page  # Add to wishlist
    assert f'id="recommended-{RG_NEBULOUS}"' in page
    assert "1 more album left out" in page
    assert "1 song not matched to an album" in page
    assert "Nothing yet" not in page  # no playlists, but recommendations
    assert client.get(f"/cover/{RG_NEBULOUS}").status_code == 200  # recommended
