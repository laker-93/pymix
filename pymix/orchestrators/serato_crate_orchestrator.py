import logging
import shutil
import zipfile
from pathlib import Path
from typing import List, Iterable

import music_tag
from pyserato.builder import Builder
from pyserato.encoders.v2_mp3_encoder import V2Mp3Encoder
from pyserato.model.crate import Crate
from pyserato.util import DuplicateTrackError

from pymix.controllers.db_controller import DbController
from pymix.controllers.rekordbox_xml_controller import RekordboxXMLController
from pymix.model.subboxplaylist import SubBoxPlaylist
from pymix.model.subboxtrack import SubBoxTrack

logger = logging.getLogger(__name__)


class SeratoCrateOrchestrator:
    def __init__(
        self,
        crate_builder: Builder,
        db_controller: DbController,
        rb_xml_controller: RekordboxXMLController,
        filebrowser_data_path_uploads: str,
        serving_music_path_base: str

    ):
        self._crate_builder = crate_builder
        self._db_controller = db_controller
        self._rb_xml_controller = rb_xml_controller
        self._filebrowser_data_path_uploads = filebrowser_data_path_uploads
        self._serving_music_path_base = serving_music_path_base
        self._mp3_encoder = V2Mp3Encoder()

    def _build_subbox_playlists(self, user: dict, crate: Crate, parent: str, subbox_playlists: List[SubBoxPlaylist]):
        name = crate.name if not parent else parent + '-' + crate.name

        if crate.tracks:
            tracks = []
            for song in crate.tracks:
                original_meta = self._db_controller.get_meta_by_user_location(user['username'], str(song.path))
                # original meta can be none if for example the track was uploaded from a standalone zip upload as this
                # method by passes the map_metadata step. However in this case, user should have not used the original
                # track in their serato/rekordbox instead upload the zip of tracks to subbox, then download them to their
                # local subbox folder and then add them to serato/rekordbox
                assert original_meta, f"no original meta found for {song.path}"
                subbox_id = original_meta['subbox_id']
                beets_path = self._rb_xml_controller.get_path_by_subbox_id(user['username'], subbox_id, False)
                entry_dir = str(beets_path).removeprefix('/music')
                src_dir = self._serving_music_path_base.format(user=user['username'])
                p = Path(src_dir + entry_dir)
                assert p.exists(), f"path {p} does not exist"
                tags = music_tag.load_file(p)
                song.path = p
                try:
                    cues = self._mp3_encoder.read_cues(song)
                except KeyError:
                    cues = None

                rating = tags.get('composer').value.count('⭐')
                tracks.append(
                    SubBoxTrack(
                        name=tags['tracktitle'].value,
                        artist=tags['artist'].value,
                        path=p,
                        album=tags['album'].value,
                        rating=rating,
                        genre=tags.get('genre').value,
                        subbox_id=subbox_id,
                        serato_hot_cues=cues
                    )
                )
            subbox_playlists.append(
                SubBoxPlaylist(
                    name=name,
                    tracks=tracks
                )
            )
        if crate.children:
            parent = name
            for child in crate.children.values():
                self._build_subbox_playlists(user, child, parent, subbox_playlists)

    def get_subbox_playlists_from_crates(self, user: dict, zip_crate_path: Path) -> List[SubBoxPlaylist]:
        """
        From the serato crates, create the internal Playlist datastructure
        """
        subbox_playlists = []

        with zipfile.ZipFile(zip_crate_path, 'r') as zip_ref:
            zip_ref.extractall(zip_crate_path.parent)
        crates = self._crate_builder.parse_crates_from_root_path(zip_crate_path.parent)
        for top_level_crate in crates.values():
            self._build_subbox_playlists(user, top_level_crate, "", subbox_playlists)
        assert subbox_playlists
        return subbox_playlists


    def create_crate(self, playlist_name: str) -> Crate:
        """
        :param playlist_name: Of the custom navidrome format <root-child-playlist>
        :return:
        """
        crate_names = playlist_name.split('-')
        root_crate = self._create_playlist_crates(crate_names)
        return root_crate

    @staticmethod
    def add_track_to_crate(user_root: str, track: SubBoxTrack, crate: Crate):
        """
        Add track to the leaf of a root crate. The crate tree must not have any branches.
        """
        while crate.children:
            crate = crate.children[-1]
        try:
            crate.add_song(
                Path(f'{user_root}/{track.path}')
            )
        except DuplicateTrackError:
            logger.info(f"track {track} is already present. Not adding to crate {crate}.")
        else:
            logger.debug(f"added track {str(track.path)} to crate {crate}")

    @staticmethod
    def _create_playlist_crates(crate_names: List[str]) -> Crate:
        """
        Returns the connected branch of crate_names.
        ["root", "child", "leaf"]
        :param crate_names: list of crate names from the root to the child
        :return: root crate
        """
        assert len(crate_names), f'must parse in a non empty list of crate_names'
        child_crate = None
        crate = None
        for crate_name in reversed(crate_names):
            crate = Crate(crate_name, children=[child_crate] if child_crate else None)
            child_crate = crate
        assert crate
        return crate

    def save(self, crate: Crate, output_path: Path):
        self._crate_builder.save(crate, output_path)
        sub_crate_path = output_path / "SubCrates"
        shutil.make_archive(output_path / "SubCrates", 'zip', sub_crate_path)
