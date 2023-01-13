import logging
from typing import List

from pymix.clients.subsonic_client import SubsonicClient
from pymix.model.subboxplaylist import SubBoxPlaylist
from pymix.model.subboxtrack import SubBoxTrack

logger = logging.getLogger(__name__)


class SubsonicOrchestrator:
    def __init__(self, subsonic_client: SubsonicClient):
        self._subsonic_client = subsonic_client

    async def _get_subsonic_playlists(self) -> List[SubBoxPlaylist]:
        """
        Creates the Playlists from Subsonic queries
        :return:
        """
        playlists = await self._subsonic_client.get_playlists()
        # get all playlists to find their ids.
        # for each playlist, get the playlist and iterate through to find the tracks
        for playlist in playlists:
            playlist.tracks = await self._subsonic_client.get_playlist_tracks(playlist.subsonic_id)
        return playlists

    async def get_subsonic_playlists(self) -> List[SubBoxPlaylist]:
        subsonic_playlists = await self._get_subsonic_playlists()
        return subsonic_playlists

    async def get_subsonic_tracks(self) -> List[SubBoxTrack]:
        subsonic_playlists = await self.get_subsonic_playlists()
        subsonic_tracks = []
        for subsonic_playlist in subsonic_playlists:
            subsonic_tracks.extend(
                subsonic_playlist.tracks
            )
        return subsonic_tracks


