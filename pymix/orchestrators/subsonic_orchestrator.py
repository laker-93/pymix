import logging
from typing import List, Set, AsyncIterator, Optional

from pymix.clients.subsonic_client import SubsonicClient
from pymix.model.subboxplaylist import SubBoxPlaylist
from pymix.model.subboxtrack import SubBoxTrack

logger = logging.getLogger(__name__)


class SubsonicOrchestrator:
    def __init__(self, subsonic_client: SubsonicClient):
        self._subsonic_client = subsonic_client

    async def _get_subsonic_playlists(self, user: dict) -> Optional[List[SubBoxPlaylist]]:
        """
        Creates internal view of the playlists and their tracks found in navirdome.
        :return:
        """
        playlists = await self._subsonic_client.get_playlists(user)
        # get all playlists to find their ids.
        # for each playlist, get the playlist and iterate through to find the tracks
        if playlists:
            for playlist in playlists:
                playlist.tracks = await self._subsonic_client.get_playlist_tracks(user, playlist.subsonic_id)
        return playlists

    async def get_subsonic_playlists(self, user: dict) -> Optional[List[SubBoxPlaylist]]:
        subsonic_playlists = await self._get_subsonic_playlists(user)
        return subsonic_playlists

    async def get_subsonic_tracks(self, user: dict) -> List[SubBoxTrack]:
        """
        Gets all tracks under playlists in subsonic
        """
        subsonic_playlists = await self.get_subsonic_playlists(user)
        subsonic_tracks = []
        if subsonic_playlists:
            for subsonic_playlist in subsonic_playlists:
                subsonic_tracks.extend(
                    subsonic_playlist.tracks
                )
        return subsonic_tracks

    async def scan(self, user: dict):
        result = await self._subsonic_client.scan(user)
        assert result

    async def create_playlists(self, user: dict, subbox_playlists: List[SubBoxPlaylist]):
        """
        Given list of subbox playlists (e.g. formed from parsing XML), create the playlist structure in navidrome.
        """
        for playlist in subbox_playlists:
            await self._subsonic_client.create_playlist(user, playlist.name, playlist.tracks)

    async def set_ratings(self, user: dict, tracks: List[SubBoxTrack]):
        """
        Given list of subbox playlists (e.g. formed from parsing XML), create the playlist structure in navidrome.
        """
        await self._subsonic_client.set_rating(user, tracks)


    async def update_tracks_with_subid(self, user: dict, subbox_playlists: Optional[List[SubBoxPlaylist]] = None, tracks: Optional[List[SubBoxTrack]] = None) -> None:
        """
        Given list of subbox playlists (e.g. formed from parsing XML), update the playlist
        track with the id of the subsonic track.
        """
        tracks_to_update = []
        if not tracks:
            for playlist in subbox_playlists:
                if playlist.tracks:
                    tracks_to_update.extend(playlist.tracks)
        else:
            tracks_to_update = tracks
        for track in tracks_to_update:
            name = track.name
            try:
                matched_track = await self._subsonic_client.get_track_match(user, title=name, artist=track.artist)
            except (KeyError, AssertionError) as ex:
                logger.warning(f'unable to find track in navidrome {track}. This track will not be imported properly. Please ensure name of track in rekordbox is correct. Exception {ex}')
            else:
                if matched_track:
                    track.sub_track_id = matched_track.sub_track_id
                else:
                    logger.warning(f'unable to find track in navidrome {track}. This track will not be imported properly. Please ensure name of track in rekordbox is correct.')

    async def get_all_tracks(self, user: dict) -> AsyncIterator[List[SubBoxTrack]]:
        return self._subsonic_client.get_all_tracks(user, 50)



