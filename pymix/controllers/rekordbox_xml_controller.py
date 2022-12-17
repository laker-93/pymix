from pathlib import Path
from typing import List, Optional

from pyrekordbox import RekordboxXml
from pyrekordbox.xml import Node

from pymix.clients.subsonic_client import SubsonicClient
from pymix.factories.rekordbox_xml_factory import RekordboxXMLFactory
from pymix.model.playlist import Playlist


class RekordboxXMLController:
    def __init__(self, subsonic_client: SubsonicClient, rekordbox_xml_factory: RekordboxXMLFactory):
        self._subsonic_client = subsonic_client
        self._rekordbox_xml_factory = rekordbox_xml_factory
        self._rekordbox_xml: Optional[RekordboxXml] = None

    def create_xml(self, xml_path: Optional[Path] = None):
        if self._rekordbox_xml is None:
            self._rekordbox_xml = self._rekordbox_xml_factory.create_rekordbox_xml(xml_path)

    @staticmethod
    def _get_folders_playlist_from_name(playlist_name: str) -> (List[str], str):
        folders_playlist = playlist_name.split('-')
        return folders_playlist[:-1], folders_playlist[-1]

    def _create_rekordbox_xml_playlist(self, playlist: Playlist):
        """
        From the playlist given, create the rekordbox folders and playlists.
        Add the tracks to the playlist.
        :param playlist:
        :return:
        """
        folder_names, playlist_name = self._get_folders_playlist_from_name(playlist.name)
        folder_name = folder_names.pop(0)
        folder = self._rekordbox_xml.add_playlist_folder(folder_name)
        # go through the folders, creating if they don't exist until reach the child folder. Then add playlist
        for folder_name in folder_names:
            folder = folder.add_playlist_folder(folder_name)
        folder.add_playlist(playlist_name)

        for track in playlist.tracks:
            track = self._rekordbox_xml.add_track(str(track.path))
            folder.add_track(track)

    async def get_playlists(self) -> List[Playlist]:
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

    async def create_rekordbox_xml_from_subsonic_playlists(self, xml_path: Path, xml_output_path: Path):
        # todo this could be made a context manager to create, update then save the xml
        self.create_xml(xml_path)
        playlists = await self.get_playlists()
        # Given the Playlist data from Subsonic create the playlist directory structure in Rekordbox.
        for playlist in playlists:
            self._create_rekordbox_xml_playlist(playlist)

        self._rekordbox_xml.save(str(xml_output_path))

    async def get_healthcheck(self) -> dict:
        return {
            'is_healthy': True
        }
