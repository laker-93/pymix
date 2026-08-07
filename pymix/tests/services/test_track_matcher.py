import asyncio
from unittest import mock

import pytest

from pymix.model.subboxtrack import SubBoxTrack
from pymix.services.track_matcher import TrackMatcher

USER = {'username': 'demoadmin'}


def _match(name):
    return (SubBoxTrack(artist='foo', album='bar', name=name, sub_track_id=1), 1.0)


@pytest.mark.anyio
async def test_resolves_each_distinct_track_once():
    # The same track reached from two playlists must cost one Navidrome lookup,
    # not two (#104).
    calls = []

    async def get_track_match(user, title, artist, album=None):
        calls.append((title, artist, album))
        return _match(title)

    client = mock.AsyncMock()
    client.get_track_match = get_track_match

    matcher = TrackMatcher(client)
    first = await matcher.match(USER, 'Volya', 'Szare', 'Action Five')
    second = await matcher.match(USER, 'Volya', 'Szare', 'Action Five')

    assert calls == [('Volya', 'Szare', 'Action Five')]
    assert first is second
    assert (matcher.n_requests, matcher.n_lookups) == (2, 1)


@pytest.mark.anyio
async def test_cache_key_ignores_case_and_surrounding_whitespace():
    calls = []

    async def get_track_match(user, title, artist, album=None):
        calls.append((title, artist, album))
        return _match(title)

    client = mock.AsyncMock()
    client.get_track_match = get_track_match

    matcher = TrackMatcher(client)
    await matcher.match(USER, 'Volya', 'Szare', 'Action Five')
    await matcher.match(USER, ' volya ', 'SZARE', 'action five')

    assert len(calls) == 1


@pytest.mark.anyio
async def test_album_is_part_of_the_cache_key():
    # get_track_match's result genuinely depends on album (#96), so two different
    # albums must not collapse onto one entry.
    client = mock.AsyncMock()
    client.get_track_match = mock.AsyncMock(side_effect=lambda user, title, artist, album=None: _match(title))

    matcher = TrackMatcher(client)
    await matcher.match(USER, 'IT', 'DJ John', 'Utopia (2007)')
    await matcher.match(USER, 'IT', 'DJ John', 'Some Other Album')
    await matcher.match(USER, 'IT', 'DJ John', None)

    assert client.get_track_match.await_count == 3


@pytest.mark.anyio
async def test_concurrent_requests_for_one_track_share_a_single_lookup():
    calls = 0

    async def get_track_match(user, title, artist, album=None):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return _match(title)

    client = mock.AsyncMock()
    client.get_track_match = get_track_match

    matcher = TrackMatcher(client)
    results = await asyncio.gather(*(matcher.match(USER, 'Volya', 'Szare', 'Action Five') for _ in range(10)))

    assert calls == 1
    assert all(r is results[0] for r in results)


@pytest.mark.anyio
async def test_distinct_lookups_overlap_up_to_the_concurrency_cap():
    in_flight = 0
    max_in_flight = 0

    async def get_track_match(user, title, artist, album=None):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return _match(title)

    client = mock.AsyncMock()
    client.get_track_match = get_track_match

    matcher = TrackMatcher(client, concurrency=4)
    await asyncio.gather(*(matcher.match(USER, f'track {i}', 'Szare', 'bar') for i in range(20)))

    # Overlapped rather than run strictly one at a time, but never above the cap —
    # an import must not stampede Navidrome with a query per track.
    assert 1 < max_in_flight <= 4


@pytest.mark.anyio
async def test_negative_result_is_cached():
    client = mock.AsyncMock()
    client.get_track_match = mock.AsyncMock(return_value=None)

    matcher = TrackMatcher(client)
    assert await matcher.match(USER, 'Nope', 'Nobody', 'bar') is None
    assert await matcher.match(USER, 'Nope', 'Nobody', 'bar') is None

    assert client.get_track_match.await_count == 1


@pytest.mark.anyio
async def test_failure_reaches_every_caller_and_is_not_retried():
    # Callers handle KeyError/AssertionError themselves (update_tracks_with_subid
    # warns and moves on), so a shared failed lookup must still raise for each of
    # them — while only costing one round trip.
    client = mock.AsyncMock()
    client.get_track_match = mock.AsyncMock(side_effect=KeyError('boom'))

    matcher = TrackMatcher(client)
    for _ in range(3):
        with pytest.raises(KeyError):
            await matcher.match(USER, 'Volya', 'Szare', 'bar')

    assert client.get_track_match.await_count == 1
