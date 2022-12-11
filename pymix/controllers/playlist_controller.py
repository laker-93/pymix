from typing import List

from pymix.clients.subsonic_client import SubsonicClient
from pymix.clients.rekordbox_client import RekordboxClient
from pymix.model.playlist import Playlist


class PlaylistController:
    def __init__(self, navidrome_client: SubsonicClient, rekordbox_client: RekordboxClient):
        self._navidrome_client = navidrome_client
        self._rekordbox_client = rekordbox_client

    def get_playlists(self) -> List[Playlist]:
        """
        Syncs the playlists from Navidrome to Rekordbox
        :return:
        """
        playlists = self._navidrome_client.get_playlists()
        # get all playlists to find their ids.
        # for each playlist, get the playlist and iterate through to find the tracks
        for playlist in playlists:
            playlist.tracks = self._navidrome_client.get_playlist_tracks(playlist.subsonic_id)
        return playlists

    def make_rekordbox_playlists(self, playlists: List[Playlist]):
        self._rekordbox_client.update_playlists(playlists)
