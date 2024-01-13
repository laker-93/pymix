import logging
import zipfile
from pathlib import Path
from typing import List, Iterable

import music_tag
from pyserato.crate import Crate, Builder
from pyserato.util import DuplicateTrackError

from pymix.model.subboxplaylist import SubBoxPlaylist
from pymix.model.subboxtrack import SubBoxTrack

logger = logging.getLogger(__name__)


class SeratoCrateOrchestrator:
    def __init__(self, crate_builder: Builder):
        self._crate_builder = crate_builder

    def _build_subbox_playlists(self, crate: Crate, parent: str, subbox_playlists: List[SubBoxPlaylist]):
        name = crate.name if not parent else parent + '-' + crate.name
        if crate.song_paths:
            tracks = []
            for song in crate.song_paths:
                tags = music_tag.load_file(str(song))
                rating = tags.get('composer').value.count('⭐')
                tracks.append(
                    SubBoxTrack(
                        name=tags['tracktitle'].value,
                        artist=tags['artist'].value,
                        path=song,
                        album=tags['album'].value,
                        rating=rating,
                        genre=tags.get('genre').value
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
            for child in crate.children:
                self._build_subbox_playlists(child, parent, subbox_playlists)

    def get_subbox_playlists_from_crates(self, zip_crate_path: Path) -> List[SubBoxPlaylist]:
        """
        From the serato crates, create the internal Playlist datastructure
        """
        subbox_playlists = []

        with zipfile.ZipFile(zip_crate_path, 'r') as zip_ref:
            zip_ref.extractall(zip_crate_path.parent)
        crates = self._crate_builder.parse_crates_from_root_path(zip_crate_path.parent / 'SubCrates')
        for top_level_crate in crates.values():
            self._build_subbox_playlists(top_level_crate, "", subbox_playlists)
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
