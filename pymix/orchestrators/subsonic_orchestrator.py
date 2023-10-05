import logging
from typing import List, Set, AsyncIterator

from pymix.clients.subsonic_client import SubsonicClient
from pymix.model.subboxplaylist import SubBoxPlaylist
from pymix.model.subboxtrack import SubBoxTrack

logger = logging.getLogger(__name__)


class SubsonicOrchestrator:
    def __init__(self, subsonic_client: SubsonicClient):
        self._subsonic_client = subsonic_client

    async def _get_subsonic_playlists(self) -> List[SubBoxPlaylist]:
        """
        Creates internal view of the playlists and their tracks found in navirdome.
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
        """
        Gets all tracks in navidrome
        """
        subsonic_playlists = await self.get_subsonic_playlists()
        subsonic_tracks = []
        for subsonic_playlist in subsonic_playlists:
            subsonic_tracks.extend(
                subsonic_playlist.tracks
            )
        return subsonic_tracks


    async def create_playlists(self, subbox_playlists: List[SubBoxPlaylist]):
        """
        Given list of subbox playlists (e.g. formed from parsing XML), create the playlist structure in navidrome.
        """
        for playlist in subbox_playlists:
            track_ids = []
            for track in playlist.tracks:
                track_ids.append(track.sub_track_id)
            await self._subsonic_client.create_playlist(playlist.name, track_ids)

    async def update_tracks_with_subid(self, subbox_playlists: List[SubBoxPlaylist]) -> None:
        """
        Given list of subbox playlists (e.g. formed from parsing XML), update the playlist
        track with the id of the subsonic track.
        """
        for playlist in subbox_playlists:
            for track in playlist.tracks:
                name = track.name
                try:
                    subsonic_track = await self._subsonic_client.query_track_by_name(name)
                except KeyError:
                    logger.warning(f'unable to find track in navidrome {track}. This track will not be imported properly. Please ensure name of track in rekordbox is correct.')
                else:
                    track.sub_track_id = subsonic_track.sub_track_id
        return 'foo'

    async def get_all_tracks(self) -> AsyncIterator[List[SubBoxTrack]]:
        return self._subsonic_client.get_all_tracks()



