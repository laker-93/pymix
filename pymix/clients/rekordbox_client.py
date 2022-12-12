from typing import List
from pathlib import Path

from pyrekordbox import RekordboxXml
from pyrekordbox.xml import Node

from pymix.model.playlist import Playlist


class RekordboxClient:
    def __init__(self, xml_path: Path):
        self._xml_path = xml_path
        self._rekordbox_xml = None

    def create_rekordbox_xml(self):
        if not self._xml_path.is_file():
            open(str(self._xml_path))
        rekordbox_xml = RekordboxXml(str(self._xml_path))
        self._rekordbox_xml = rekordbox_xml

    @staticmethod
    def _get_folders_playlist_from_name(playlist_name: str) -> (List[str], str):
        folders_playlist = playlist_name.split('-')
        return folders_playlist[:-1], folders_playlist[-1]

    def _update_playlist(self, playlist: Playlist):
        folder_names, playlist_name = self._get_folders_playlist_from_name(playlist.name)
        folder_name = folder_names.pop(0)
        folder = self._rekordbox_xml.add_playlist_folder(folder_name)
        # go through the folders, creating if they don't exist until reach the child folder. Then add playlist
        for folder_name in folder_names:
            folder = folder.add_playlist_folder(folder_name)
        folder.add_playlist(playlist_name)


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