import datetime
from pathlib import PosixPath
from typing import List
from unittest import mock
import pytest

from pymix.tests.fixtures.container import container  # noqa
from pymix.model.subboxplaylist import SubBoxPlaylist
from pymix.model.subboxtrack import SubBoxTrack


async def mock_get_playlist_tracks(subsonic_id) -> List[SubBoxTrack]:
    if subsonic_id == '4b421baf-21b7-4238-966e-c8f03e5dd5c2':
        tracks = [SubBoxTrack(name='Volya', path=PosixPath('Szare/Volya _ Action Five/Volya.mp3'))]
    elif subsonic_id == '43fdb143-4b87-4224-a50c-fef2e2ce9763':
        tracks = [SubBoxTrack(name='Flagship', path=PosixPath('Blu Peter/[Unknown Album]/Flagship.mp3'))]
    elif subsonic_id == '99015bb5-cc58-4492-a5ee-6108f3acba41':
        tracks = [SubBoxTrack(name='Hush Now', path=PosixPath('Nene H/Beast EP/Hush Now.mp3')),
                  SubBoxTrack(name='Hush Now (VTSS Remix)',
                              path=PosixPath('Nene H/Beast EP/Hush Now (VTSS Remix).mp3')),
                  SubBoxTrack(name='So Cute!! (Schiere Remix)',
                              path=PosixPath('Ayako Mori/So Cute!!/So Cute!! (Schiere Remix).mp3')),
                  SubBoxTrack(name='Apnea (Original mix)',
                              path=PosixPath('Oisel/Entroterra EP/Apnea (Original mix).mp3'))
                  ]
    else:
        raise Exception(f'subsonic id {subsonic_id} has no tracks hardcoded in mock function')
    return tracks


@pytest.fixture
def mock_playlists() -> List[SubBoxPlaylist]:
    return [
        SubBoxPlaylist(name='UK-Funky', n_of_songs=1, comment='',
                       last_updated=datetime.datetime(2022, 12, 15, 12, 56, 39, tzinfo=datetime.timezone.utc),
                       duration_s=371, subsonic_id='4b421baf-21b7-4238-966e-c8f03e5dd5c2',
                       ),
        SubBoxPlaylist(name='hardcore', n_of_songs=1, comment='lofi',
                       last_updated=datetime.datetime(2022, 12, 4, 8, 40, 30, tzinfo=datetime.timezone.utc),
                       duration_s=377, subsonic_id='43fdb143-4b87-4224-a50c-fef2e2ce9763',
                       ),
        SubBoxPlaylist(name='techno-dark', n_of_songs=4, comment='',
                       last_updated=datetime.datetime(2022, 12, 15, 14, 24, 42, tzinfo=datetime.timezone.utc),
                       duration_s=1373, subsonic_id='99015bb5-cc58-4492-a5ee-6108f3acba41',
                       )
    ]


@pytest.mark.anyio
async def test_create_rekordbox_xml_from_subsonic_playlists(container, mock_playlists):
    mock_subsonic_client = mock.AsyncMock()
    mock_subsonic_client.get_playlists = mock.AsyncMock(return_value=mock_playlists)
    mock_subsonic_client.get_playlist_tracks = mock_get_playlist_tracks

    mock_rekordbox_xml_factory = mock.MagicMock()

    mock_xml_path = 'foo'
    mock_xml_output_path = 'foo'
    with container.subsonic_client.override(
            mock_subsonic_client
    ) as _, container.rekordbox_xml_factory.override(
        mock_rekordbox_xml_factory
    ):
        # todo not sure why this resource needs awaiting?
        rekordbox_xml_controller = await container.rekordbox_xml_controller()
        await rekordbox_xml_controller.create_rekordbox_xml_from_subsonic_playlists(mock_xml_path, mock_xml_output_path)
