from unittest import mock
import pytest

from pymix.orchestrators.subsonic_orchestrator import SubsonicOrchestrator
from pymix.tests.fixtures.container import container  # noqa


@pytest.mark.anyio
async def test_create_rekordbox_xml_from_subsonic_playlists(container, mock_playlist_a, playlist_a_tracks,
                                                            mock_playlist_b, playlist_b_tracks, mock_playlist_c,
                                                            playlist_c_tracks):
    mock_subsonic_orchestrator = mock.Mock(spec=SubsonicOrchestrator)
    mock_playlist_a.tracks = playlist_a_tracks
    mock_playlist_b.tracks = playlist_b_tracks
    mock_playlist_c.tracks = playlist_c_tracks
    mock_subsonic_orchestrator.get_subsonic_playlists = mock.AsyncMock(
        return_value=[
            mock_playlist_a,
            mock_playlist_b,
            mock_playlist_c
        ]
    )
    mock_subsonic_orchestrator.get_subsonic_tracks = mock.AsyncMock(
        return_value=playlist_a_tracks + playlist_b_tracks + playlist_c_tracks
    )

    mock_rekordbox_xml_factory = mock.MagicMock()
    mock_rekordbox_xml_orchestrator = mock.MagicMock()
    mock_rekordbox_xml_orchestrator.get_all_xml_tracks = mock.MagicMock(return_value=playlist_a_tracks)

    mock_xml_path = 'foo'
    mock_xml_output_path = 'foo'
    with container.subsonic_orchestrator.override(
            mock_subsonic_orchestrator
    ) as _, container.rekordbox_xml_factory.override(
        mock_rekordbox_xml_factory
    ) as _, container.rekordbox_xml_orchestrator.override(
        mock_rekordbox_xml_orchestrator
    ):
        # todo not sure why this resource needs awaiting?
        rekordbox_xml_controller = await container.rekordbox_xml_controller()
        await rekordbox_xml_controller.create_rekordbox_xml_from_subsonic_playlists(mock_xml_path, mock_xml_output_path)
    assert mock_rekordbox_xml_orchestrator.add_track_to_rekordbox_playlist.call_count == len(
        playlist_a_tracks + playlist_b_tracks + playlist_c_tracks
    )
