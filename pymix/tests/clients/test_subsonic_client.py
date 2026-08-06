import datetime
from unittest.mock import AsyncMock, MagicMock
import pytest

import pymix.clients.subsonic_client as subsonic_client_module
from pymix.clients.subsonic_client import (
    SubsonicClient,
    _split_title,
    score_track_match,
)
from pymix.model.subboxplaylist import SubBoxPlaylist
from pymix.model.subboxtrack import SubBoxTrack

mock_subsonic_response = {'subsonic-response': {'status': 'ok', 'version': '1.16.1', 'type': 'navidrome',
                           'serverVersion': '0.48.0 (af5c2b5a)', 'playlists': {'playlist': [
            {'id': 'e824f4a8-2815-4f9d-87aa-0b8a84d02845', 'name': 'ambient-light', 'songCount': 1, 'duration': 229,
             'public': False, 'owner': 'lajp', 'created': '2022-11-29T18:22:08.1811484Z',
             'changed': '2022-11-29T18:23:43.3112555Z'},
            {'id': 'fc052e84-1ee7-4e26-8180-911cc928c759', 'name': 'grime', 'comment': 'dark', 'songCount': 1,
             'duration': 210, 'public': False, 'owner': 'lajp', 'created': '2022-11-27T10:57:01.1365012Z',
             'changed': '2022-11-29T18:24:03.1492699Z'},
            {'id': '99015bb5-cc58-4492-a5ee-6108f3acba41', 'name': 'techno-dark', 'songCount': 0, 'duration': 0,
             'public': True, 'owner': 'lajp', 'created': '2022-11-29T18:23:08.7731483Z',
             'changed': '2022-11-29T18:23:08.7732488Z'}]}}
                          }

@pytest.mark.anyio
async def test_get_playlist():
    subsonic_client = SubsonicClient(
        "http://{user}:{port}", MagicMock(), "mock_version", "foo", "bar", None, "test"
    )
    subsonic_client.get = AsyncMock(return_value=mock_subsonic_response)
    expected_playlists = [
        SubBoxPlaylist(name='ambient-light', n_of_songs=1, comment='',
                       last_updated=datetime.datetime(2022, 11, 29, 18, 23, 43, 311255, tzinfo=datetime.timezone.utc),
                       duration_s=229, subsonic_id='e824f4a8-2815-4f9d-87aa-0b8a84d02845'),
        SubBoxPlaylist(name='grime', n_of_songs=1, comment='dark',
                       last_updated=datetime.datetime(2022, 11, 29, 18, 24, 3, 149269, tzinfo=datetime.timezone.utc),
                       duration_s=210, subsonic_id='fc052e84-1ee7-4e26-8180-911cc928c759'),
        SubBoxPlaylist(name='techno-dark', n_of_songs=0, comment='',
                       last_updated=datetime.datetime(2022, 11, 29, 18, 23, 8, 773248, tzinfo=datetime.timezone.utc),
                       duration_s=0, subsonic_id='99015bb5-cc58-4492-a5ee-6108f3acba41')
    ]
    playlists = await subsonic_client.get_playlists({"username": "lajp", "password": "pw"})
    assert expected_playlists == playlists


# --- get_now_playing ---------------------------------------------------------------

@pytest.mark.anyio
async def test_get_now_playing_returns_entries():
    subsonic_client = SubsonicClient(
        "http://{user}:{port}", MagicMock(), "mock_version", "foo", "bar", None, "test"
    )
    subsonic_client.get = AsyncMock(return_value={
        'subsonic-response': {
            'status': 'ok',
            'nowPlaying': {'entry': [{'id': 'abc', 'minutesAgo': 2}, {'id': 'def', 'minutesAgo': 5}]},
        }
    })
    entries = await subsonic_client.get_now_playing({"username": "lajp", "password": "pw"})
    assert entries == [{'id': 'abc', 'minutesAgo': 2}, {'id': 'def', 'minutesAgo': 5}]


@pytest.mark.anyio
async def test_get_now_playing_normalises_a_single_entry_to_a_list():
    # Subsonic's XML->JSON serialisation can collapse a one-item entry list to a
    # bare dict rather than a one-element list.
    subsonic_client = SubsonicClient(
        "http://{user}:{port}", MagicMock(), "mock_version", "foo", "bar", None, "test"
    )
    subsonic_client.get = AsyncMock(return_value={
        'subsonic-response': {'status': 'ok', 'nowPlaying': {'entry': {'id': 'abc', 'minutesAgo': 1}}}
    })
    entries = await subsonic_client.get_now_playing({"username": "lajp", "password": "pw"})
    assert entries == [{'id': 'abc', 'minutesAgo': 1}]


@pytest.mark.anyio
async def test_get_now_playing_returns_empty_list_when_nobody_playing():
    subsonic_client = SubsonicClient(
        "http://{user}:{port}", MagicMock(), "mock_version", "foo", "bar", None, "test"
    )
    subsonic_client.get = AsyncMock(return_value={'subsonic-response': {'status': 'ok', 'nowPlaying': {}}})
    entries = await subsonic_client.get_now_playing({"username": "lajp", "password": "pw"})
    assert entries == []


def _client():
    # _find_best_match / _clean_track_for_match / get_track_match's tier control need no
    # network — the constructor args are irrelevant to the matching logic under test.
    return SubsonicClient(MagicMock(), MagicMock(), "mock_version", "foo", "bar", None, "test")


def _track(name, artist="Burial", album=None):
    return SubBoxTrack(name=name, artist=artist, album=album)


# --- _split_title -----------------------------------------------------------------

@pytest.mark.parametrize("raw,core,qualifier", [
    ("Rodent (Kode9 remix)", "rodent", "kode9 remix"),
    ("Distant Lights (Kode9 remix)", "distant lights", "kode9 remix"),
    ("Rodent", "rodent", ""),
    ("Rodent [VIP Remix]", "rodent", "vip remix"),
    # A title that is *only* a parenthetical falls back to the full cleaned title for the
    # core, so the gate never runs against an empty string.
    ("(Intro)", "intro", "intro"),
])
def test_split_title(raw, core, qualifier):
    assert _split_title(raw) == (core, qualifier)


# --- score_track_match ------------------------------------------------------------

def _score(title_a, title_b, artist_a="burial", artist_b="burial", album_a=None, album_b=None):
    core_a, qual_a = _split_title(title_a)
    core_b, qual_b = _split_title(title_b)
    return score_track_match(core_a, qual_a, artist_a, album_a, core_b, qual_b, artist_b, album_b)


def test_core_gate_rejects_different_song_even_with_exact_artist_and_qualifier():
    # Issue #42: same artist, coincidentally the same remix qualifier, different song.
    # The core titles ("rodent" vs "distant lights") diverge, so it is rejected outright
    # regardless of the exact artist match and the shared "(Kode9 remix)".
    assert _score("Rodent (Kode9 remix)", "Distant Lights (Kode9 remix)") is None


def test_qualifier_ranks_right_version_above_original_and_wrong_remix():
    want = "Rodent (Kode 9 remix)"
    exact = _score(want, "Rodent (Kode 9 remix)")
    vip = _score(want, "Rodent (VIP remix)")
    original = _score(want, "Rodent")
    # All are the same core song, so all match; the qualifier ranks the exact version top.
    assert exact == 1.0
    assert exact > vip > original
    # The original is a soft rank-down, not a rejection — still matchable on its own.
    assert original is not None


def test_missing_qualifier_still_matches_plain_titles():
    assert _score("Rodent", "Rodent") == 1.0


# --- _find_best_match -------------------------------------------------------------

@pytest.mark.anyio
async def test_find_best_match_rejects_same_artist_wrong_song():
    client = _client()
    # Even at a very loose threshold the core gate keeps the wrong song out.
    match = await client._find_best_match(
        "Rodent (Kode9 remix)", "Burial",
        [_track("Distant Lights (Kode9 remix)")], None, similarity_threshold=0.4,
    )
    assert match is None


@pytest.mark.anyio
async def test_find_best_match_picks_exact_remix_over_original():
    client = _client()
    candidates = [_track("Rodent"), _track("Rodent (Kode 9 remix)"), _track("Rodent (VIP remix)")]
    match = await client._find_best_match(
        "Rodent (Kode 9 remix)", "Burial", candidates, None, similarity_threshold=0.6,
    )
    assert match is not None
    matched_track, score = match
    assert matched_track.name == "Rodent (Kode 9 remix)"
    assert score == 1.0


@pytest.mark.anyio
async def test_find_best_match_falls_back_to_original_when_only_candidate():
    client = _client()
    match = await client._find_best_match(
        "Rodent (Kode 9 remix)", "Burial", [_track("Rodent")], None, similarity_threshold=0.6,
    )
    assert match is not None
    matched_track, _score = match
    assert matched_track.name == "Rodent"


# --- get_track_match tiers --------------------------------------------------------

@pytest.mark.anyio
async def test_get_track_match_max_tier_2_skips_token_search():
    client = _client()
    client.query_tracks_by = AsyncMock(return_value=[])
    client.query_track_by_name = AsyncMock(return_value=[])
    match = await client.get_track_match(
        {"username": "u", "password": "p"}, "Rodent", "Burial", None,
        max_tier=2, min_confidence=0.8,
    )
    assert match is None
    # Tier 1 uses query_tracks_by; tier 2 uses query_track_by_name once (title only). The
    # token tier (which would call query_track_by_name per token) must not run.
    client.query_tracks_by.assert_awaited_once()
    client.query_track_by_name.assert_awaited_once_with({"username": "u", "password": "p"}, "Rodent")


# --- set_rating -------------------------------------------------------------------

def _rated(sub_track_id, rating):
    t = SubBoxTrack(name=f"t{sub_track_id}", artist="Burial", album=None)
    t.sub_track_id = sub_track_id
    t.rating = rating
    return t


@pytest.mark.anyio
async def test_set_rating_issues_one_call_per_rated_track_and_never_sleeps(monkeypatch):
    """
    One `setRating` per rated track, and no delay between them.

    This loop used to `asyncio.sleep(1)` after every call, which cost one second per
    rated track and dominated a Rekordbox import (~99s of a ~121s, 99-track import).
    Navidrome has no such rate limit — it accepts the writes back to back — and
    `setRating` takes a single id, so there is no batch call to collapse this into.
    Assert the sleep stays gone rather than trusting a comment.
    """
    slept = []

    async def _fail_on_sleep(delay, *args, **kwargs):
        slept.append(delay)

    monkeypatch.setattr(subsonic_client_module.asyncio, "sleep", _fail_on_sleep)

    client = SubsonicClient(
        "http://{user}:{port}", MagicMock(), "mock_version", "foo", "bar", None, "test"
    )
    client.get = AsyncMock(return_value={'subsonic-response': {'status': 'ok'}})

    # The unrated track must be skipped entirely.
    tracks = [_rated(1, 5), _rated(2, 3), _rated(3, 4), _track("unrated")]
    await client.set_rating({"username": "lajp", "password": "pw"}, tracks)

    assert client.get.await_count == 3
    assert slept == []
    called = [c.args[0] for c in client.get.await_args_list]
    for song_id, rating in ((1, 5), (2, 3), (3, 4)):
        assert any(f"id={song_id}" in u and f"rating={rating}" in u for u in called), called


@pytest.mark.anyio
async def test_set_rating_logs_and_continues_when_one_write_fails():
    """A single rejected rating must not abort the rest of the pass."""
    client = SubsonicClient(
        "http://{user}:{port}", MagicMock(), "mock_version", "foo", "bar", None, "test"
    )
    client.get = AsyncMock(side_effect=[
        {'subsonic-response': {'status': 'ok'}},
        {'subsonic-response': {'status': 'failed', 'error': {'code': 70}}},
        {'subsonic-response': {'status': 'ok'}},
    ])

    await client.set_rating(
        {"username": "lajp", "password": "pw"},
        [_rated(1, 5), _rated(2, 3), _rated(3, 4)],
    )

    assert client.get.await_count == 3

