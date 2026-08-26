import logging
import shutil
import zipfile
from pathlib import Path
from typing import Dict, List, Iterable, Optional

import music_tag
from pyserato.builder import Builder
from pyserato.encoders.v2_mp3_encoder import V2Mp3Encoder
from pyserato.model.crate import Crate
from pyserato.model.track import Track
from pyserato.util import DuplicateTrackError

from pymix.controllers.db_controller import DbController
from pymix.controllers.rekordbox_xml_controller import RekordboxXMLController
from pymix.model.serato_import import CrateImportReport, SkippedCrateTrack
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
        serving_music_path_base: str,
        local_user_music_stem: str

    ):
        self._crate_builder = crate_builder
        self._db_controller = db_controller
        self._rb_xml_controller = rb_xml_controller
        self._filebrowser_data_path_uploads = filebrowser_data_path_uploads
        self._serving_music_path_base = serving_music_path_base
        self._local_user_music_stem = local_user_music_stem
        self._mp3_encoder = V2Mp3Encoder()

    def _get_user_music_root(self, username: str) -> Path:
        if '{user}' in self._local_user_music_stem:
            return Path('/' + self._local_user_music_stem.format(user=username))
        return Path('/' + self._local_user_music_stem) / username

    def _resolve_track_location(self, user_root: str, username: str, track: SubBoxTrack) -> Path:
        src_dir = self._get_user_music_root(username)
        track_path = Path(track.path)
        try:
            relative_path = track_path.relative_to(src_dir)
            return Path(user_root) / relative_path
        except ValueError:
            logger.warning(
                'track path %s is not under src_dir %s for user %s; falling back to legacy path join',
                track_path,
                src_dir,
                username,
            )
            return Path(f'{user_root}/{track.path}')

    def _resolve_subbox_id(
        self,
        username: str,
        crate_path: str,
        identities: Dict[str, str],
    ) -> Optional[str]:
        """
        Which subbox track is this crate entry?

        A `.crate` file stores an absolute path on the user's machine and nothing
        else, and pymix never sees that file, so the path is the only key we have
        and it is not an identity — a Serato user moves and renames files.

        1. The client's manifest. It has the local file, so it can read SUBBOX_ID
           straight off it, which survives the move.
        2. `user_location`, written by /sync/map_meta during an upload. Covers the
           Rekordbox-first user, and any track uploaded in this same import.

        Returns None when neither knows the path; the caller skips the track and
        records why rather than taking the whole import down with it.
        """
        subbox_id = identities.get(crate_path)
        if subbox_id:
            return subbox_id

        original_meta = self._db_controller.get_meta_by_user_location(username, crate_path)
        if original_meta:
            return original_meta['subbox_id']

        return None

    def _build_subbox_playlists(
        self,
        user: dict,
        crate: Crate,
        parent_components: List[str],
        subbox_playlists: List[SubBoxPlaylist],
        identities: Dict[str, str],
        report: CrateImportReport,
    ):
        components = parent_components + [crate.name]

        if crate.tracks:
            tracks = []
            for song in crate.tracks:
                crate_path = str(song.path)

                subbox_id = self._resolve_subbox_id(user['username'], crate_path, identities)
                if not subbox_id:
                    # Not an error: the user has crates full of records they have
                    # never uploaded to subbox, and that is the normal state of a
                    # Serato library. Leave the track out of the playlist and say so.
                    logger.info('no subbox identity for crate entry %s; skipping', crate_path)
                    report.skipped.append(
                        SkippedCrateTrack(crate_path, 'not in your subbox library')
                    )
                    continue

                beets_path = self._rb_xml_controller.get_path_by_subbox_id(user['username'], subbox_id, False)
                entry_dir = str(beets_path).removeprefix('/music')
                src_dir = f'{self._serving_music_path_base}/{user["username"]}'
                p = Path(src_dir + entry_dir)
                if not p.exists():
                    # The id is known but beets has no file behind it — the import
                    # of that track failed, or it was deleted since.
                    logger.warning(
                        'subbox_id %s for crate entry %s resolves to %s, which does not exist',
                        subbox_id, crate_path, p,
                    )
                    report.skipped.append(
                        SkippedCrateTrack(crate_path, 'no file in your library for that track')
                    )
                    continue

                tags = music_tag.load_file(p)
                song.path = p
                try:
                    cues = self._mp3_encoder.read_cues(song)
                except KeyError:
                    cues = None

                rating = tags.get('composer').value.count('⭐')
                report.matched += 1
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
            # A crate whose every track was skipped becomes an empty playlist,
            # which is worse than no playlist -- it looks like subbox lost the
            # tracks. The skips are reported separately.
            if tracks:
                display_name = " / ".join(components)
                subbox_playlists.append(
                    SubBoxPlaylist(
                        name=display_name,
                        tracks=tracks,
                        path_components=components,
                    )
                )
        if crate.children:
            for child in crate.children.values():
                self._build_subbox_playlists(user, child, components, subbox_playlists, identities, report)

    def get_subbox_playlists_from_crates(
        self,
        user: dict,
        zip_crate_path: Path,
        identities: Optional[Dict[str, str]] = None,
    ) -> tuple[List[SubBoxPlaylist], CrateImportReport]:
        """
        From the serato crates, create the internal Playlist datastructure.

        ``identities`` maps a crate entry's stored path to its subbox_id, as
        resolved by the client -- see pymix.model.serato_import.
        """
        subbox_playlists: List[SubBoxPlaylist] = []
        report = CrateImportReport()

        with zipfile.ZipFile(zip_crate_path, 'r') as zip_ref:
            zip_ref.extractall(zip_crate_path.parent)
        crates = self._crate_builder.parse_crates_from_root_path(zip_crate_path.parent)
        report.crates_parsed = len(crates)
        if not crates:
            # parse_crates_from_root_path uses iterdir(), not rglob(), so a zip
            # whose .crate files sit inside a folder (what Finder's "Compress"
            # produces) parses to nothing. Say that, rather than failing three
            # steps later on an empty playlist list.
            raise ValueError(
                f'no crates found in {zip_crate_path.name}. The .crate files must be at '
                f'the root of the zip, not inside a folder.'
            )

        for top_level_crate in crates.values():
            self._build_subbox_playlists(user, top_level_crate, [], subbox_playlists, identities or {}, report)

        report.playlists_built = len(subbox_playlists)
        if not subbox_playlists:
            raise ValueError(
                f'none of the {report.total} tracks in your {report.crates_parsed} crates are in '
                f'your subbox library, so there was nothing to build playlists from.'
            )
        return subbox_playlists, report


    def create_crate(self, playlist: SubBoxPlaylist) -> Crate:
        """
        Creates a crate tree from a SubBoxPlaylist.
        Uses path_components if available for lossless folder reconstruction,
        otherwise falls back to splitting display name by ' / '.
        """
        if playlist.path_components:
            crate_names = list(playlist.path_components)
        else:
            crate_names = playlist.name.split(' / ')
        root_crate = self._create_playlist_crates(crate_names)
        return root_crate

    def add_track_to_crate(self, user_root: str, username: str, track: SubBoxTrack, crate: Crate):
        """
        Add track to the leaf of a root crate. The crate tree must not have any branches.
        """
        while crate.children:
            crate = list(crate.children.values())[-1]
        try:
            resolved_location = self._resolve_track_location(user_root, username, track)
            crate.add_track(
                Track.from_path(resolved_location)
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
            crate = Crate(crate_name, children={child_crate.name: child_crate} if child_crate else None)
            child_crate = crate
        assert crate
        return crate

    def save(self, crate: Crate, output_path: Path):
        self._crate_builder.save(crate, output_path)
        sub_crate_path = output_path / "SubCrates"
        shutil.make_archive(output_path / "SubCrates", 'zip', sub_crate_path)
