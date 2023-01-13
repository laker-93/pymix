import logging
from pathlib import Path
from typing import List, Optional

from pyrekordbox import RekordboxXml
from pyrekordbox.xml import Node, Track

from pymix.factories.rekordbox_xml_factory import RekordboxXMLFactory
from pymix.model.subboxplaylist import SubBoxPlaylist
from pymix.model.subboxtrack import SubBoxTrack

logger = logging.getLogger(__name__)


class RekordboxXMLOrchestrator:
    def __init__(self, rekordbox_xml_factory: RekordboxXMLFactory):
        self._rekordbox_xml_factory = rekordbox_xml_factory
        self._rekordbox_xml: Optional[RekordboxXml] = None

    def create_xml(self, xml_path: Optional[Path] = None):
        if self._rekordbox_xml is None:
            self._rekordbox_xml = self._rekordbox_xml_factory.create_rekordbox_xml(xml_path)

    @staticmethod
    def _get_folders_playlist_from_name(playlist_name: str) -> (List[str], str):
        folders_playlist = playlist_name.split('-')
        return folders_playlist[:-1], folders_playlist[-1]

    def get_subbox_playlists_from_rekordbox_xml_playlists(self, xml_playlists: List[Node]) -> List[SubBoxPlaylist]:
        """
        From the rekordbox XML playlists, create the internal Playlist datastructure
        :param xml_playlists:
        :param rekordbox_playlists:
        :return:
        """
        rekordbox_playlists: List[SubBoxPlaylist] = []
        for playlist in xml_playlists:
            name = playlist.name
            track_ids = playlist.get_tracks()
            if not playlist.is_playlist:
                # recurse through the folder structure until reach the playlist leaves
                playlists = playlist.get_playlists()
                self.get_subbox_playlists_from_rekordbox_xml_playlists(playlists, rekordbox_playlists)
                continue
            assert playlist.key_type == 'TrackID', f"playlist key type {playlist.key_type}"
            tracks = []
            for track_id in track_ids:
                track = self._rekordbox_xml.get_track(TrackID=track_id)
                tracks.append(
                    SubBoxTrack(
                        name=track.Name,
                        artist=track.Artist,
                        path=Path(track.Location),
                        album=track.Album,
                        genre=track.Genre,
                        track_id=track_id
                    )
                )
            rekordbox_playlists.append(
                SubBoxPlaylist(
                    name=name,
                    tracks=tracks
                )
            )
        return rekordbox_playlists

    def create_rekordbox_xml_playlist(self, playlist_name: str) -> Node:
        """
        :param playlist_name: Of the custom navidrom format <genre-subgenre-playlist>
        :return:
        """
        folders, playlist_name = self._get_folders_playlist_from_name(playlist_name)
        playlist_root = self._create_playlist_folders(folders) if folders else self._rekordbox_xml
        new_playlist = playlist_root.add_playlist(playlist_name)
        return new_playlist

    def add_track_to_rekordbox_playlist(self, track: SubBoxTrack, playlist: Node):
        try:
            rekordbox_track = self._rekordbox_xml.add_track(
                str(track.path),
                Name=track.name,
                Artist=track.artist,
                Album=track.album,
                Genre=track.genre
            )
            logger.debug(f"added track {str(track.path)}")
        except ValueError:
            track_id = track.track_id
            # must have the track_id set since the subsonic track must've necessarily been found in the rekordbox
            # collection for this exception to have occurred.
            assert track_id, f"track id none for {track}"
            rekordbox_track = self._rekordbox_xml.get_track(TrackID=track_id)
            logger.debug(f"track already present, found at {str(rekordbox_track)}")
        logger.debug(f"track {rekordbox_track} added to {playlist}")
        playlist.add_track(rekordbox_track.TrackID)


    def _get_playlist_folder(self, playlist_folder_name: str) -> Optional[Node]:
        playlists = self._rekordbox_xml._root_node.get_playlists()
        playlist_folder = None
        for playlist in playlists:
            if playlist.name == playlist_folder_name and playlist.is_folder:
                playlist_folder = playlist
        return playlist_folder

    def _create_playlist_folders(self, folder_names: List[str]) -> Node:
        folder_name = folder_names.pop(0)
        # todo extend pyrekordbox to provide a get_playlist_folder api that uses the below code
        playlist_folder = self._get_playlist_folder(folder_name)
        if not playlist_folder:
            playlist_folder = self._rekordbox_xml.add_playlist_folder(folder_name)
        # go through the folders, creating if they don't exist until reach the child folder. Then add playlist
        for folder_name in folder_names:
            playlist_folder = playlist_folder.add_playlist_folder(folder_name)
        return playlist_folder

    def get_all_xml_playlists(self) -> List[Node]:
        all_playlists: List[Node] = self._rekordbox_xml.root_playlist_folder.get_playlists()
        return all_playlists

    def get_all_xml_tracks(self) -> List[SubBoxTrack]:
        all_tracks = []
        for rekordbox_track in self._rekordbox_xml.get_tracks():
            all_tracks.append(
                SubBoxTrack(
                    name=rekordbox_track.Name,
                    artist=rekordbox_track.Artist,
                    path=Path(rekordbox_track.Location),
                    album=rekordbox_track.Album,
                    genre=rekordbox_track.Genre,
                    track_id=rekordbox_track.TrackID
                )
            )
        return all_tracks

    def save_xml(self, xml_output_path: Path):
        self._rekordbox_xml.save(str(xml_output_path))

