import asyncio
from unittest import mock
import pytest

from pymix.orchestrators.subsonic_orchestrator import SubsonicOrchestrator
from pymix.tests.fixtures.container import container  # noqa


@pytest.mark.anyio
async def test_get_subsonic_playlists(container, mock_playlists, mock_get_playlist_tracks,
                                      playlist_a_tracks, playlist_b_tracks, playlist_c_tracks):
    mock_subsonic_client = mock.AsyncMock()
    mock_subsonic_client.get_playlists = mock.AsyncMock(return_value=mock_playlists)
    mock_subsonic_client.get_playlist_tracks = mock_get_playlist_tracks

    with container.subsonic_client.override(
            mock_subsonic_client
    ):
        subsonic_orchestrator = await container.subsonic_orchestrator()
    subsonic_tracks = await subsonic_orchestrator.get_subsonic_tracks()
    expected_tracks = playlist_a_tracks + playlist_b_tracks + playlist_c_tracks
    assert subsonic_tracks == expected_tracks


# Instantiated directly (SubsonicOrchestrator's constructor takes just the client,
# no DI needed) rather than through the `container` fixture used above, which
# requires a live DB connection to resolve — these only exercise the scoping/
# concurrency behaviour of _get_subsonic_playlists itself.

@pytest.mark.anyio
async def test_get_subsonic_playlists_scopes_fetch_to_playlist_ids(
    mock_playlist_a, mock_playlist_b, mock_playlist_c, playlist_b_tracks
):
    fetched_ids = []

    async def get_playlist_tracks(user, subsonic_id):
        fetched_ids.append(subsonic_id)
        return playlist_b_tracks

    mock_subsonic_client = mock.AsyncMock()
    mock_subsonic_client.get_playlists = mock.AsyncMock(
        return_value=[mock_playlist_a, mock_playlist_b, mock_playlist_c]
    )
    mock_subsonic_client.get_playlist_tracks = get_playlist_tracks

    orchestrator = SubsonicOrchestrator(mock_subsonic_client)
    playlists = await orchestrator.get_subsonic_playlists(
        user={'username': 'demoadmin'}, playlist_ids={mock_playlist_b.subsonic_id}
    )

    assert [p.subsonic_id for p in playlists] == [mock_playlist_b.subsonic_id]
    assert playlists[0].tracks == playlist_b_tracks
    # Only the requested playlist's tracks were fetched — a and c were never
    # touched, unlike the old behaviour which fetched every playlist's tracks
    # before filtering down to the ones actually requested.
    assert fetched_ids == [mock_playlist_b.subsonic_id]


@pytest.mark.anyio
async def test_get_subsonic_playlists_no_filter_returns_all(
    mock_playlist_a, mock_playlist_b, mock_playlist_c, playlist_a_tracks, playlist_b_tracks, playlist_c_tracks
):
    track_map = {
        mock_playlist_a.subsonic_id: playlist_a_tracks,
        mock_playlist_b.subsonic_id: playlist_b_tracks,
        mock_playlist_c.subsonic_id: playlist_c_tracks,
    }

    async def get_playlist_tracks(user, subsonic_id):
        return track_map[subsonic_id]

    mock_subsonic_client = mock.AsyncMock()
    mock_subsonic_client.get_playlists = mock.AsyncMock(
        return_value=[mock_playlist_a, mock_playlist_b, mock_playlist_c]
    )
    mock_subsonic_client.get_playlist_tracks = get_playlist_tracks

    orchestrator = SubsonicOrchestrator(mock_subsonic_client)
    playlists = await orchestrator.get_subsonic_playlists(user={'username': 'demoadmin'})

    assert {p.subsonic_id: p.tracks for p in playlists} == {
        mock_playlist_a.subsonic_id: playlist_a_tracks,
        mock_playlist_b.subsonic_id: playlist_b_tracks,
        mock_playlist_c.subsonic_id: playlist_c_tracks,
    }


@pytest.mark.anyio
async def test_get_subsonic_playlists_fetches_tracks_concurrently(
    mock_playlist_a, mock_playlist_b, mock_playlist_c, playlist_a_tracks, playlist_b_tracks, playlist_c_tracks
):
    track_map = {
        mock_playlist_a.subsonic_id: playlist_a_tracks,
        mock_playlist_b.subsonic_id: playlist_b_tracks,
        mock_playlist_c.subsonic_id: playlist_c_tracks,
    }
    in_flight = 0
    max_in_flight = 0

    async def get_playlist_tracks(user, subsonic_id):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return track_map[subsonic_id]

    mock_subsonic_client = mock.AsyncMock()
    mock_subsonic_client.get_playlists = mock.AsyncMock(
        return_value=[mock_playlist_a, mock_playlist_b, mock_playlist_c]
    )
    mock_subsonic_client.get_playlist_tracks = get_playlist_tracks

    orchestrator = SubsonicOrchestrator(mock_subsonic_client)
    await orchestrator.get_subsonic_playlists(user={'username': 'demoadmin'})

    # All 3 fetches should have overlapped rather than run strictly one at a time.
    assert max_in_flight > 1
