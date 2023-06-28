from unittest import mock
import pytest

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
