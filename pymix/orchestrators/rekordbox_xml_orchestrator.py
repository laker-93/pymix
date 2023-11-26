import logging
import os
from pathlib import Path
from typing import List, Optional

from pyrekordbox import RekordboxXml
from pyrekordbox.xml import Node, Track, RATING_MAPPING

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

    def get_track_by_id(self, track_id: int) -> Track:
        return self._rekordbox_xml.get_track(TrackID=track_id)

    @staticmethod
    def _get_folders_playlist_from_name(playlist_name: str) -> (List[str], str):
        folders_playlist = playlist_name.split('-')
        return folders_playlist[:-1], folders_playlist[-1]

    def get_subbox_playlists_from_rekordbox_xml_playlists(self, xml_playlists: List[Node], parent: str, subbox_playlists) -> List[SubBoxPlaylist]:
        """
        From the rekordbox XML playlists, create the internal Playlist datastructure
        :param xml_playlists:
        :param rekordbox_playlists:
        :return:
        """
        for playlist in xml_playlists:
            name = playlist.name if not parent else parent + '-' + playlist.name
            track_ids = playlist.get_tracks()
            if not playlist.is_playlist:
                # recurse through the folder structure until reach the playlist leaves
                playlists = playlist.get_playlists()
                self.get_subbox_playlists_from_rekordbox_xml_playlists(playlists, name, subbox_playlists)
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
                        rating=track.Rating,
                        track_id=track_id
                    )
                )
            subbox_playlists.append(
                SubBoxPlaylist(
                    name=name,
                    tracks=tracks
                )
            )

    def create_rekordbox_xml_playlist(self, playlist_name: str) -> Node:
        """
        :param playlist_name: Of the custom navidrome format <genre-subgenre-playlist>
        :return:
        """
        folders, playlist_name = self._get_folders_playlist_from_name(playlist_name)
        playlist_root = self._create_playlist_folders(folders) if folders else self._rekordbox_xml
        new_playlist = playlist_root.add_playlist(playlist_name)
        return new_playlist

    def add_track(self, track: SubBoxTrack, suppress_error: bool=False) -> None:
        """
        Adds the track to the XML. Optionally suppress the error if the track is already present.
        :param track:
        :param suppress_error:
        :return:
        """
        try:
            rekord_track = self._rekordbox_xml.add_track(
                str(track.path),
                Name=track.name,
                Artist=track.artist,
                Album=track.album,
                Rating=RATING_MAPPING.inverse[track.rating],
                Genre=track.genre
            )
        except ValueError as ex:
            if not suppress_error:
                raise ValueError(f'unable to add track {track}') from ex
        else:
            logger.debug(f"added track {str(track.path)} {rekord_track}")


    def add_track_to_rekordbox_playlist(self, user_root: str, track: SubBoxTrack, playlist: Node):
        try:
            rekordbox_track = self._rekordbox_xml.add_track(
                f'{user_root}/{track.path}',
                Name=track.name,
                Artist=track.artist,
                Album=track.album,
                Rating=RATING_MAPPING.inverse[track.rating],
                Genre=track.genre
            )
            logger.debug(f"added track {str(track.path)}")
        except ValueError:
            track_id = track.track_id
            # if the track_id is set then the subsonic track is already present in the rekordbox xml,
            # otherwise the track has yet to be added to rekordbox xml and appears in multiple playlists.
            if track_id:
                rekordbox_track = self._rekordbox_xml.get_track(TrackID=track_id)
            else:
                # the rekord box get_track api is stupid so do some very inefficient work around
                #rekordbox_track = self._rekordbox_xml.get_track(index=1, Location=os.path.normpath(str(track.path)))
                for other in self._rekordbox_xml.get_tracks():
                    if os.path.normpath(os.path.normpath(str(track.path))) == other.Location:
                        rekordbox_track = other
                        break
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

