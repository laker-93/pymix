import logging
from pathlib import Path
from typing import List, Optional

from pyrekordbox import RekordboxXml
from pyrekordbox.xml import Node

from pymix.clients.subsonic_client import SubsonicClient
from pymix.factories.rekordbox_xml_factory import RekordboxXMLFactory
from pymix.model.playlist import Playlist
from pymix.model.track import Track

logger = logging.getLogger(__name__)


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

    def _get_rekordbox_xml_playlists(self, root_playlists, rekordbox_playlists: List[Playlist]) -> None:
        for playlist in root_playlists:
            name = playlist.name
            track_ids = playlist.get_tracks()
            if not playlist.is_playlist:
                playlists = playlist.get_playlists()
                self._get_rekordbox_xml_playlists(playlists, rekordbox_playlists)
                continue
            assert playlist.key_type == 'TrackID', f"playlist key type {playlist.key_type}"
            tracks = []
            for track_id in track_ids:
                track = self._rekordbox_xml.get_track(TrackID=track_id)
                tracks.append(
                    Track(
                        name=track.Name,
                        artist=track.Artist,
                        path=Path(track.Location),
                        album=track.Album,
                        genre=track.Genre,
                        track_id=track_id
                    )
                )
            rekordbox_playlists.append(
                Playlist(
                    name=name,
                    tracks=tracks
                )
            )

    def _create_rekordbox_xml_playlist(self, subsonic_playlist: Playlist):
        """
        From the playlist given, create the rekordbox folders and playlists.
        Add the tracks to the playlist.
        :param subsonic_playlist:
        :return:
        """
        folders, playlist_name = self._get_folders_playlist_from_name(subsonic_playlist.name)
        playlist_root = self._create_playlist_folders(folders) if folders else self._rekordbox_xml
        new_playlist = playlist_root.add_playlist(playlist_name)

        for track in subsonic_playlist.tracks:
            try:
                rekordbox_track = self._rekordbox_xml.add_track(
                    str(track.path),
                    Name=track.name,
                    Artist=track.artist,
                    Album=track.album,
                    Genre=track.genre
                )
                logger.info(f"added track {str(track.path)}")
            except ValueError:
                track_id = track.track_id
                # must have the track_id set since the subsonic track must've necessarily been found in the rekordbox
                # collection for this exception to have occurred.
                assert track_id, f"track id none for {track}"
                rekordbox_track = self._rekordbox_xml.get_track(TrackID=track_id)
                logger.info(f"track already present, found at {str(rekordbox_track)}")
            logger.info(f"track {rekordbox_track} added to {new_playlist}")
            new_playlist.add_track(rekordbox_track.TrackID)

    def _create_playlist_folders(self, folder_names):
        folder_name = folder_names.pop(0)
        folder = self._rekordbox_xml.add_playlist_folder(folder_name)
        # go through the folders, creating if they don't exist until reach the child folder. Then add playlist
        for folder_name in folder_names:
            folder = folder.add_playlist_folder(folder_name)
        return folder

    async def get_subsonic_playlists(self) -> List[Playlist]:
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


        subsonic_playlists = await self.get_subsonic_playlists()
        subsonic_tracks = []
        for subsonic_playlist in subsonic_playlists:
            subsonic_tracks.extend(
                subsonic_playlist.tracks
            )


        root_playlists = self._rekordbox_xml.root_playlist_folder.get_playlists()
        rekordbox_playlists = []
        self._get_rekordbox_xml_playlists(root_playlists, rekordbox_playlists)
        rekordbox_tracks_unparsed = self._rekordbox_xml.get_tracks()
        rekordbox_tracks = []
        for rekordbox_track in rekordbox_tracks_unparsed:
            rekordbox_tracks.append(
                Track(
                    name=rekordbox_track.Name,
                    artist=rekordbox_track.Artist,
                    path=Path(rekordbox_track.Location),
                    album=rekordbox_track.Album,
                    genre=rekordbox_track.Genre,
                    track_id=rekordbox_track.TrackID
                )
            )


        # If a track in the subsonic set is already present in rekordbox then must remove it before its playlist can be
        # updated. Need the rekordbox TrackID to do this. Therefore, for those subsonic tracks that are already in
        # rekordbox, take the TrackID from the rekordbox set so they can be dealt with.
        # Given the Playlist data from Subsonic create the playlist directory structure in Rekordbox.
        for subsonic_track in subsonic_tracks:
            for rekordbox_track in rekordbox_tracks:
                if subsonic_track == rekordbox_track:
                    logger.info(f"found subsonic track {subsonic_track} in rekordbox. Setting track id to {rekordbox_track.track_id}")
                    subsonic_track.track_id = rekordbox_track.track_id

        for subsonic_playlist in subsonic_playlists:
            self._create_rekordbox_xml_playlist(subsonic_playlist)

        # todo remove any playlists that have no tracks
        self._rekordbox_xml.save(str(xml_output_path))

    async def get_healthcheck(self) -> dict:
        return {
            'is_healthy': True
        }
