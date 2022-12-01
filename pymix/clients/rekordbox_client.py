from typing import List
from pathlib import Path

from pyrekordbox import RekordboxXml
from pyrekordbox.xml import Node

from pymix.model.playlist import Playlist


class RekordboxClient:
    def __init__(self, xml_path: Path):
        self._xml_path = xml_path
        self._rekordbox_xml = RekordboxXml(str(self._xml_path))

    @staticmethod
    def _get_folders_from_name(playlist_name: str) -> List[str]:
        return playlist_name.split('-')[:-1]

    def _update_playlist(self, playlist: Playlist):
        folders = self._get_folders_from_name(playlist.name)
        for folder in folders:
            pass
            # go through folders calling
            # folder = xml.add_playlist_folder("folder")
            # folder = folder.add_playlist_folder("folder")
            # until get to the child. Then
        # folder.add_playlist(playlist_name)


    def _create_folders(self, folders: List[str]) -> Node:
        """
        Gets the child folder node of the folders.
        If any of the folders in the path don't exist, then create them.
        :param folders:
        :return:
        """

    def update_playlists(self, playlists: List[Playlist]):
        """
        Given the Playlist data from Subsonic create the playlist directory structure in Rekordbox.
        :param playlists:
        :return:
        """
        for playlist in playlists:
            self._update_playlist(playlist)