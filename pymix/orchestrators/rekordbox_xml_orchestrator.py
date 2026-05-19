import logging
import os.path
from pathlib import Path
from typing import List, Optional, Dict
from xml.etree.ElementTree import ElementTree, indent

from pyrekordbox.rbxml import Node, RATING_MAPPING, XmlDuplicateError

from pymix.controllers.db_controller import DbController
from pymix.factories.rekordbox_xml_factory import RekordboxXMLFactory
from pymix.model.subboxplaylist import SubBoxPlaylist
from pymix.model.subboxtrack import SubBoxTrack
from pymix.utils.get_duration import get_duration
from pymix.utils.tag_subbox_id import get_subbox_id

logger = logging.getLogger(__name__)


class RekordboxXMLOrchestrator:
    def __init__(self, rekordbox_xml_factory: RekordboxXMLFactory, db_controller: DbController, local_user_music_stem: str):
        self._rekordbox_xml_factory = rekordbox_xml_factory
        self._db_controller = db_controller
        self._local_user_music_stem = local_user_music_stem.removesuffix('/')
        self._rekordbox_xml = None

    def _get_user_music_root(self, username: str) -> Path:
        if '{user}' in self._local_user_music_stem:
            return Path('/' + self._local_user_music_stem.format(user=username))
        return Path('/' + self._local_user_music_stem) / username

    def _resolve_track_location(self, user_root: str, user: dict, track: SubBoxTrack) -> Path:
        src_dir = self._get_user_music_root(user['username'])
        track_path = Path(track.path)
        try:
            relative_path = track_path.relative_to(src_dir)
            return Path(user_root) / relative_path
        except ValueError:
            logger.warning(
                'track path %s is not under src_dir %s for user %s; falling back to legacy path join',
                track_path,
                src_dir,
                user['username'],
            )
            return Path(f'{user_root}/{track.path}')

    def create_xml(self, xml_path: Optional[Path] = None):
        self._rekordbox_xml = self._rekordbox_xml_factory.create_rekordbox_xml(xml_path)

    def get_track_by_id(self, track_id: int) -> SubBoxTrack:
        rb_track = self._rekordbox_xml.get_track(TrackID=track_id)
        return SubBoxTrack(
            name=rb_track.Name,
            artist=rb_track.Artist,
            album=rb_track.Album,
            path=Path(rb_track.Location),
            track_number=rb_track.TrackNumber,
        )

    def get_subbox_playlists_from_rekordbox_xml_playlists(self, xml_playlists: List[Node], parent_components: List[str], subbox_playlists) -> List[SubBoxPlaylist]:
        """
        From the rekordbox XML playlists, create the internal Playlist datastructure
        :param xml_playlists:
        :param parent_components: list of parent folder names leading to this level
        :param subbox_playlists:
        :return:
        """
        for playlist in xml_playlists:
            components = parent_components + [playlist.name]
            track_ids = playlist.get_tracks()
            if not playlist.is_playlist:
                # recurse through the folder structure until reach the playlist leaves
                playlists = playlist.get_playlists()
                self.get_subbox_playlists_from_rekordbox_xml_playlists(playlists, components, subbox_playlists)
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
            display_name = " / ".join(components)
            subbox_playlists.append(
                SubBoxPlaylist(
                    name=display_name,
                    tracks=tracks,
                    path_components=components,
                )
            )

    def create_rekordbox_xml_playlist(self, subsonic_playlist: SubBoxPlaylist) -> Node:
        """
        From the playlist, create the rekordbox folders and playlist node.
        Uses path_components if available for lossless folder reconstruction,
        otherwise falls back to splitting display name by ' / '.
        """
        if subsonic_playlist.path_components and len(subsonic_playlist.path_components) > 1:
            folders = subsonic_playlist.path_components[:-1]
            playlist_name = subsonic_playlist.path_components[-1]
        else:
            parts = subsonic_playlist.name.split(' / ')
            folders = parts[:-1]
            playlist_name = parts[-1]
        playlist_root = self._create_playlist_folders(list(folders)) if folders else self._rekordbox_xml
        new_playlist = playlist_root.add_playlist(playlist_name)
        logger.info(f'created playlist with name {playlist_name}')
        return new_playlist


    def _get_cue_data(self, user: dict, track: SubBoxTrack) -> Optional[Dict]:
        subbox_id = get_subbox_id(track.pymix_path)
        if subbox_id is None:
            logger.warning(f'no subbox id found for track {track}. This track will be imported without cues or loops.')
            return None
        lib_entry = self._db_controller.get_library_entry(user['username'], subbox_id)
        if lib_entry:
            cue_data = lib_entry["cuedata"]
            return cue_data

    def add_track_to_rekordbox_playlist(self, user_root: str, user: dict, track: SubBoxTrack, playlist: Node, force: bool = True):
        """
        Add track in playlist. Optionally force the track in to playlist even if the track is already in the XML.
        """
        rekordbox_track = None
        logger.info(track.name)
        logger.info(f'attempting to add track {track} to playlist {playlist}')
        cue_data = self._get_cue_data(user, track)
        duration = get_duration(track.pymix_path)
        resolved_location = self._resolve_track_location(user_root, user, track)
        resolved_location_str = os.path.normpath(str(resolved_location))
        try:
            rekordbox_track = self._rekordbox_xml.add_track(
            resolved_location_str,
                Name=track.name,
                Artist=track.artist,
                Album=track.album,
                Rating=RATING_MAPPING.inverse[track.rating],
                Genre=track.genre
            )
            logger.info(f'got track {rekordbox_track}')
            logger.debug(f"added track {str(track.path)}")
        except XmlDuplicateError:
            track_id = track.track_id
            # if the track_id is set then the subsonic track is already present in the rekordbox xml,
            # otherwise the track has yet to be added to rekordbox xml and appears in multiple playlists.
            if track_id:
                rekordbox_track = self._rekordbox_xml.get_track(TrackID=track_id)
            else:
                #rekordbox_track = self._rekordbox_xml.get_track(Location=os.path.normpath(f'{user_root}/{track.path}'))
                # the rekord box get_track api is stupid so do some very inefficient work around
                #rekordbox_track = self._rekordbox_xml.get_track(index=1, Location=os.path.normpath(str(track.path)))
                for other in self._rekordbox_xml.get_tracks():
                    if resolved_location_str == other.Location:
                        rekordbox_track = other
                        break
            assert rekordbox_track
            if force:
                logger.info(f"track already present, found at {str(rekordbox_track)}")
                playlist.add_track(rekordbox_track.TrackID)
                logger.info(f"track {rekordbox_track} added to {playlist}")
            else:
                logger.info(f"track {track} is already present. Not forcefully adding to playlist {playlist}.")
        else:
            playlist.add_track(rekordbox_track.TrackID)
            logger.info(f"track {rekordbox_track} from {track} added to {playlist}")
            rekordbox_track["TotalTime"] = duration
            if cue_data:
                cues = cue_data.get("cues", [])
                loops = cue_data.get("loops", [])

                # Limits from Serato
                num_loops = min(len(loops), 4)
                num_cues = min(len(cues), 8)

                # Add cues first (starting from slot 0)
                for i, cue in enumerate(cues[:num_cues]):
                    rekordbox_track.add_mark(
                        Name=cue["name"],
                        Type="cue",
                        Start=cue["position"] / 1000,
                        Num=i
                    )

                # Add loops at the end
                loop_start_num = 8 - num_loops
                for i, loop in enumerate(loops[:num_loops]):
                    rekordbox_track.add_mark(
                        Name=loop.get("name", ""),
                        Type="loop",
                        Start=loop["start"] / 1000,
                        End=loop["end"] / 1000,
                        Num=loop_start_num + i
                    )

        assert rekordbox_track


    def _get_playlist_folder(self, playlist_folder_name: str, parent_folder: Optional[Node] = None) -> Optional[Node]:
        playlists = self._rekordbox_xml._root_node.get_playlists() if parent_folder is None else parent_folder.get_playlists()
        playlist_folder = None
        for playlist in playlists:
            if playlist.name == playlist_folder_name and playlist.is_folder:
                playlist_folder = playlist
                break
        return playlist_folder

    def _create_playlist_folders(self, folder_names: List[str], parent_folder: Optional[Node]=None) -> Node:
        """
        Given the folder names (e.g. ['root-folder', 'mid-folder', 'child-folder'])
        return the child folder Node with the correct structure.
        If ['root-folder', 'mid-folder'] already exists -> add 'child-folder' as a new node
        If 'root-folder' does not exist, create new folders.
        """
        folder_name = folder_names.pop(0)
        playlist_folder = self._get_playlist_folder(folder_name, parent_folder)
        if not playlist_folder:
            if parent_folder:
                playlist_folder = parent_folder.add_playlist_folder(folder_name)
            else:
                playlist_folder = self._rekordbox_xml.add_playlist_folder(folder_name)
        if not folder_names:
            return playlist_folder
        else:
            return self._create_playlist_folders(
                folder_names,
                playlist_folder
            )

    def get_all_xml_playlists(self) -> List[Node]:
        all_playlists: List[Node] = self._rekordbox_xml.root_playlist_folder.get_playlists()
        return all_playlists

    def get_playlist(self, name: str) -> Optional[Node]:
        try:
            playlist = self._rekordbox_xml.get_playlist(name)
            # force an exception if the playlist does not exist
            str(playlist)
            assert playlist.is_playlist
        except Exception:
            playlist = None
        return playlist

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
                    track_id=rekordbox_track.TrackID,
                    rating=rekordbox_track.Rating,
                    bpm=rekordbox_track.AverageBpm
                )
            )
        return all_tracks

    def save_xml(self, xml_output_path: Path):
        tree = ElementTree(self._rekordbox_xml._root)
        indent(tree, space="\t", level=0)
        tree.write(xml_output_path, encoding='utf-8', xml_declaration=True)
        logger.info(f'saved xml to {xml_output_path}')
