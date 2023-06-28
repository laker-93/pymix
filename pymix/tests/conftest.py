import datetime
from pathlib import PosixPath
from typing import List

import pytest

from pymix.model.subboxplaylist import SubBoxPlaylist
from pymix.model.subboxtrack import SubBoxTrack

@pytest.fixture
def anyio_backend():
    return 'asyncio'

@pytest.fixture
def playlist_a_tracks():
    return [SubBoxTrack(artist='foo', album='bar', name='Volya', path=PosixPath('Szare/Volya _ Action Five/Volya.mp3'))]


@pytest.fixture
def playlist_b_tracks():
    return [SubBoxTrack(artist='foo', album='bar', name='Flagship',
                        path=PosixPath('Blu Peter/[Unknown Album]/Flagship.mp3'))]


@pytest.fixture
def playlist_c_tracks():
    return [SubBoxTrack(artist='foo', album='bar', name='Hush Now', path=PosixPath('Nene H/Beast EP/Hush Now.mp3')),
            SubBoxTrack(artist='foo', album='bar', name='Hush Now (VTSS Remix)',
                        path=PosixPath('Nene H/Beast EP/Hush Now (VTSS Remix).mp3')),
            SubBoxTrack(artist='foo', album='bar', name='So Cute!! (Schiere Remix)',
                        path=PosixPath('Ayako Mori/So Cute!!/So Cute!! (Schiere Remix).mp3')),
            SubBoxTrack(artist='foo', album='bar', name='Apnea (Original mix)',
                        path=PosixPath('Oisel/Entroterra EP/Apnea (Original mix).mp3'))
            ]


@pytest.fixture
def mock_get_playlist_tracks(playlist_a_tracks, playlist_b_tracks, playlist_c_tracks):
    async def _mock_get_playlist_tracks(subsonic_id) -> List[SubBoxTrack]:
        if subsonic_id == '4b421baf-21b7-4238-966e-c8f03e5dd5c2':
            tracks = playlist_a_tracks
        elif subsonic_id == '43fdb143-4b87-4224-a50c-fef2e2ce9763':
            tracks = playlist_b_tracks
        elif subsonic_id == '99015bb5-cc58-4492-a5ee-6108f3acba41':
            tracks = playlist_c_tracks
        else:
            raise Exception(f'subsonic id {subsonic_id} has no tracks hardcoded in mock function')
        return tracks
    return _mock_get_playlist_tracks


@pytest.fixture
def mock_playlist_a():
    return SubBoxPlaylist(name='UK-Funky', n_of_songs=1, comment='',
                   last_updated=datetime.datetime(2022, 12, 15, 12, 56, 39, tzinfo=datetime.timezone.utc),
                   duration_s=371, subsonic_id='4b421baf-21b7-4238-966e-c8f03e5dd5c2',
                   )

@pytest.fixture
def mock_playlist_b():
    return SubBoxPlaylist(name='hardcore', n_of_songs=1, comment='lofi',
               last_updated=datetime.datetime(2022, 12, 4, 8, 40, 30, tzinfo=datetime.timezone.utc),
               duration_s=377, subsonic_id='43fdb143-4b87-4224-a50c-fef2e2ce9763',
               )

@pytest.fixture
def mock_playlist_c():
    return SubBoxPlaylist(name='techno-dark', n_of_songs=4, comment='',
                   last_updated=datetime.datetime(2022, 12, 15, 14, 24, 42, tzinfo=datetime.timezone.utc),
                   duration_s=1373, subsonic_id='99015bb5-cc58-4492-a5ee-6108f3acba41',
                   )

@pytest.fixture
def mock_playlists(mock_playlist_a, mock_playlist_b, mock_playlist_c) -> List[SubBoxPlaylist]:
    return [mock_playlist_a, mock_playlist_b, mock_playlist_c]
