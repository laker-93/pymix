from pymix.clients.navidrome_client import NavidromeClient
from pymix.clients.rekordbox_client import RekordboxClient


class PlaylistController:
    def __init__(self, navidrome_client: NavidromeClient, rekordbox_client: RekordboxClient):
        self._navidrome_client = navidrome_client
        self._rekordbox_client = rekordbox_client

    def sync_playlists(self):
        """
        Syncs the playlists from Navidrome to Rekordbox
        :return:
        """
        playlists = self._navidrome_client.get_playlists()
        self._rekordbox_client.update_playlists(playlists)
