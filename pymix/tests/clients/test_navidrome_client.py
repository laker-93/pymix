import datetime
from unittest.mock import AsyncMock, MagicMock, Mock
import pytest

from pymix.clients.navidrome_client import NavidromeClient
from pymix.model.playlist import Playlist

mock_navidrome_response = {'subsonic-response': {'status': 'ok', 'version': '1.16.1', 'type': 'navidrome',
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

@pytest.mark.asyncio
async def test_get_playlist():
    navidrome_client = NavidromeClient(MagicMock(), MagicMock(), "mock_username", "mock_version")
    navidrome_client.get = AsyncMock(return_value=mock_navidrome_response)
    expected_playlists = [
        Playlist(name='ambient-light', n_of_songs=1, comment='',
                 last_updated=datetime.datetime(2022, 11, 29, 18, 23, 43, 311255, tzinfo=datetime.timezone.utc),
                 duration_s=229),
        Playlist(name='grime', n_of_songs=1, comment='dark',
                 last_updated=datetime.datetime(2022, 11, 29, 18, 24, 3, 149269, tzinfo=datetime.timezone.utc),
                 duration_s=210),
        Playlist(name='techno-dark', n_of_songs=0, comment='',
                 last_updated=datetime.datetime(2022, 11, 29, 18, 23, 8, 773248, tzinfo=datetime.timezone.utc),
                 duration_s=0)
    ]
    playlists = await navidrome_client.get_playlists()
    assert expected_playlists == playlists

