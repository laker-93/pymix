import logging
from typing import List, Set, AsyncIterator

from pymix.clients.subsonic_client import SubsonicClient
from pymix.model.subboxplaylist import SubBoxPlaylist
from pymix.model.subboxtrack import SubBoxTrack

logger = logging.getLogger(__name__)


class SubsonicOrchestrator:
    def __init__(self, subsonic_client: SubsonicClient):
        self._subsonic_client = subsonic_client

    async def _get_subsonic_playlists(self, user_root: str, user: dict) -> List[SubBoxPlaylist]:
        """
        Creates internal view of the playlists and their tracks found in navirdome.
        :return:
        """
        playlists = await self._subsonic_client.get_playlists(user)
        # get all playlists to find their ids.
        # for each playlist, get the playlist and iterate through to find the tracks
        for playlist in playlists:
            playlist.tracks = await self._subsonic_client.get_playlist_tracks(user_root, user, playlist.subsonic_id)
        return playlists

    async def get_subsonic_playlists(self, user_root: str, user: dict) -> List[SubBoxPlaylist]:
        subsonic_playlists = await self._get_subsonic_playlists(user_root, user)
        return subsonic_playlists

    async def get_subsonic_tracks(self, user_root: str, user: dict) -> List[SubBoxTrack]:
        """
        Gets all tracks under playlists in subsonic
        """
        subsonic_playlists = await self.get_subsonic_playlists(user_root, user)
        subsonic_tracks = []
        for subsonic_playlist in subsonic_playlists:
            subsonic_tracks.extend(
                subsonic_playlist.tracks
            )
        return subsonic_tracks

    async def scan(self, user: dict):
        result = await self._subsonic_client.scan(user)
        assert result

    async def create_playlists_and_set_rating(self, user: dict, subbox_playlists: List[SubBoxPlaylist]):
        """
        Given list of subbox playlists (e.g. formed from parsing XML), create the playlist structure in navidrome.
        """
        for playlist in subbox_playlists:
            await self._subsonic_client.create_playlist(user, playlist.name, playlist.tracks)
            await self._subsonic_client.set_rating(user, playlist.tracks)

    async def update_tracks_with_subid(self, user_root: str, user: dict, subbox_playlists: List[SubBoxPlaylist]) -> None:
        """
        Given list of subbox playlists (e.g. formed from parsing XML), update the playlist
        track with the id of the subsonic track.
        """
        for playlist in subbox_playlists:
            for track in playlist.tracks:
                name = track.name
                try:
                    subsonic_track = await self._subsonic_client.query_track_by_name(user_root, user, name)
                except KeyError as ex:
                    logger.warning(f'unable to find track in navidrome {track}. This track will not be imported properly. Please ensure name of track in rekordbox is correct. Exception {ex}')
                else:
                    track.sub_track_id = subsonic_track.sub_track_id
        return 'foo'

    async def get_all_tracks(self, user_root: str, user: dict) -> AsyncIterator[List[SubBoxTrack]]:
        return self._subsonic_client.get_all_tracks(user_root, user, 50)



