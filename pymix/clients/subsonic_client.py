import logging
import string
import hashlib
import random
from pathlib import Path
from typing import Tuple, List, Optional

import aiohttp

from pymix.model.subboxplaylist import SubBoxPlaylist

from toredocore.providers.base_api_client import BaseAPIClient

from pymix.model.subboxtrack import SubBoxTrack
from pymix.utils.utility import add_url_params

logger = logging.getLogger(__name__)


class SubsonicClient(BaseAPIClient):
    def __init__(self, host: str, session: aiohttp.ClientSession, username: str, version: str, music_path_base_to_add: str, music_path_base_to_remove: str):
        super().__init__(host, session)
        self._username = username
        self._version = version
        self._music_path_base_to_add = music_path_base_to_add.rstrip('/')
        cleaned_music_path_base_to_remove = '/' + music_path_base_to_remove.rstrip('/').lstrip('/')
        self._music_path_base_to_remove = cleaned_music_path_base_to_remove

    @staticmethod
    def _calculate_token() -> Tuple[str, str]:
        """
        generate random salt of 6 chars
        :return: tuple(token, salt)
        """
        letters = string.ascii_lowercase
        salt = ''.join(random.choice(letters) for _ in range(6))
        return hashlib.md5(
            f"konichiwalajp!{salt}".encode("utf-8")
        ).hexdigest(), salt


    def _subsonic_format_url(self, url: str, params: Optional[dict] = None) -> str:
        """
        example:
        http://localhost:4533/rest/getStarred.view?u=lajp&p=konichiwalajp!&v=1.16.1&c=myapp
        http://your-server/rest/ping.view?u=joe&t=26719a1196d2a940705a59634eb18eab&s=c19b2d&v=1.12.0&c=myapp
        :param url:
        :return:
        """
        token, salt = self._calculate_token()
        required_params = {
            "u": self._username,
            "t": token,
            "s": salt,
            "v": self._version,
            "c": "myapp",
            "f": "json"
        }
        if params:
            required_params.update(params)

        url = add_url_params(url + ".view?", required_params)
        return url

    @staticmethod
    def _parse_playlists(response: dict) -> List[SubBoxPlaylist]:
        resp_playlists = response['subsonic-response']['playlists']['playlist']
        return [
            SubBoxPlaylist(
                name=playlist['name'],
                n_of_songs=playlist['songCount'],
                comment=playlist.get('comment', ''),
                last_updated=playlist['changed'],
                duration_s=playlist['duration'],
                subsonic_id=playlist['id']
            ) for playlist in resp_playlists
        ]

    def _parse_tracks(self, response: dict) -> List[SubBoxTrack]:
        resp_playlist = response['subsonic-response']['playlist']['entry']
        return [
            SubBoxTrack(
                name=entry['title'],
                artist=entry['artist'],
                path=Path(f"{self._music_path_base_to_add}/{entry['path'].lstrip(self._music_path_base_to_remove)}"),
                album=entry['album'],
                genre=entry.get('genre')
            ) for entry in resp_playlist
        ]

    async def get_playlists(self) -> List[SubBoxPlaylist]:
        url = self._subsonic_format_url(f"{self._host}/rest/getPlaylists")
        response = await self.get(url)
        assert response
        result = self._parse_playlists(response)
        return result

    async def create_playlists(self, subbox_playlists: List[SubBoxPlaylist]):
        for playlist in subbox_playlists:
            _id = None
            self._subsonic_format_url(f"{self._host}/rest/createPlaylist", params={"name": playlist.name, "songId": _id})

    async def get_playlist_tracks(self, playlist_id: str) -> List[SubBoxTrack]:
        url = self._subsonic_format_url(f"{self._host}/rest/getPlaylist", params={"id": playlist_id})
        response = await self.get(url)
        assert response
        tracks = self._parse_tracks(response)
        return tracks

    async def get_track(self, track_id: str):
        url = self._subsonic_format_url(f"{self._host}/rest/getSong", params={"id": track_id})
        response = await self.get(url)
        return response

    async def query(self):
        url = self._subsonic_format_url(f"{self._host}/rest/search2", params={"query": "Burial"})
        response = await self.get(url)
        return response
