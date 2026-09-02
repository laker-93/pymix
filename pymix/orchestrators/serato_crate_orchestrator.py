import logging
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

import music_tag
from pyserato.builder import Builder
from pyserato.encoders.v2_mp3_encoder import V2Mp3Encoder
from pyserato.model.crate import Crate
from pyserato.model.hot_cue import HotCue

from pymix.controllers.db_controller import DbController
from pymix.controllers.rekordbox_xml_controller import RekordboxXMLController
from pymix.model.serato_import import (
    CrateImportReport,
    SeratoTrackIdentity,
    SkippedCrateTrack,
)
from pymix.model.subboxplaylist import SubBoxPlaylist
from pymix.model.subboxtrack import SubBoxTrack

logger = logging.getLogger(__name__)


class SeratoCrateOrchestrator:
    # The formats subbox can read Serato's cues out of on the server. Serato
    # itself stores Markers2 in FLAC (a base64'd Vorbis comment), WAV and AIFF
    # (an ID3 chunk, byte-identical to the MP3 GEOB frame) and M4A (a freeform
    # atom) as well, but pyserato only ships an MP3 encoder, so for anything else
    # the server has no reading to offer -- laker-93/pyserato#12.
    _CUE_READABLE_SUFFIXES = frozenset({'.mp3'})

    def __init__(
        self,
        crate_builder: Builder,
        db_controller: DbController,
        rb_xml_controller: RekordboxXMLController,
        serving_music_path_base: str,
    ):
        self._crate_builder = crate_builder
        self._db_controller = db_controller
        self._rb_xml_controller = rb_xml_controller
        self._serving_music_path_base = serving_music_path_base
        self._mp3_encoder = V2Mp3Encoder()

    def _resolve_identity(
        self,
        username: str,
        crate_path: str,
        identities: Dict[str, SeratoTrackIdentity],
    ) -> Optional[SeratoTrackIdentity]:
        """
        Which subbox track is this crate entry?

        A `.crate` file stores an absolute path on the user's machine and nothing
        else, and pymix never sees that file, so the path is the only key we have
        and it is not an identity — a Serato user moves and renames files.

        1. The client's manifest. It has the local file, so it can read SUBBOX_ID
           straight off it, which survives the move — and, since it has the file,
           the file's current cues too.
        2. `user_location`, written by /sync/map_meta during an upload. Covers the
           Rekordbox-first user, and any track uploaded in this same import. No
           cues: all this knows is which track it is.

        Returns None when neither knows the path; the caller skips the track and
        records why rather than taking the whole import down with it.
        """
        identity = identities.get(crate_path)
        if identity:
            return identity

        original_meta = self._db_controller.get_meta_by_user_location(username, crate_path)
        if original_meta:
            return SeratoTrackIdentity(
                crate_path=crate_path, subbox_id=original_meta['subbox_id']
            )

        return None

    def _read_cues_from_library_copy(self, path: Path, song) -> Optional[List[HotCue]]:
        """
        The cues on the server's own copy of the file, or None if it has none to give.

        Only reached when the client sent no cues for this crate entry, which is
        also how "this client cannot read cues off this format" arrives -- the
        client omits the field rather than sending an empty list, because an empty
        list means "read them, there were none" and would clear what subbox holds.

        Never fatal. A track whose cues subbox cannot read still belongs in the
        playlist, and this is the line that used to take the whole import down: one
        FLAC anywhere in the crate tree raised HeaderNotFoundError out of a
        background task, *after* every track had already uploaded and *before* a
        single playlist was built, so the user was left with their whole library
        imported, no playlists, and an "Import Failed" screen telling them to
        upload again (#145).
        """
        if path.suffix.lower() not in self._CUE_READABLE_SUFFIXES:
            # Not a failure and not worth a warning: most of a real library is
            # formats we cannot read cues from, and they import fine without them.
            logger.debug('no serato cue reader for %s; importing it without cues', path)
            return None
        try:
            return self._mp3_encoder.read_cues(song)
        except KeyError:
            # An MP3 Serato has never analysed: no Markers2 frame at all.
            return None
        except Exception:
            # A .mp3 that is not one, a truncated file, or a Markers2 blob this
            # decoder does not understand. One unreadable track is not a reason to
            # fail the import of every other one.
            logger.warning(
                'could not read serato cues from %s; importing it without them',
                path, exc_info=True,
            )
            return None

    def _build_subbox_playlists(
        self,
        user: dict,
        crate: Crate,
        parent_components: List[str],
        subbox_playlists: List[SubBoxPlaylist],
        identities: Dict[str, SeratoTrackIdentity],
        report: CrateImportReport,
    ):
        components = parent_components + [crate.name]

        if crate.tracks:
            tracks = []
            for song in crate.tracks:
                crate_path = str(song.path)

                identity = self._resolve_identity(user['username'], crate_path, identities)
                if not identity:
                    # Not an error: the user has crates full of records they have
                    # never uploaded to subbox, and that is the normal state of a
                    # Serato library. Leave the track out of the playlist and say so.
                    logger.info('no subbox identity for crate entry %s; skipping', crate_path)
                    report.skipped.append(
                        SkippedCrateTrack(crate_path, 'not in your subbox library')
                    )
                    continue

                subbox_id = identity.subbox_id
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

                try:
                    tags = music_tag.load_file(p)
                except Exception:
                    # In the library, but not parseable at all: a truncated upload,
                    # or an extension that lies about the container it holds. One
                    # such file costs the user that track, not the import (#145).
                    logger.warning(
                        'could not read tags from %s for crate entry %s; skipping it',
                        p, crate_path, exc_info=True,
                    )
                    report.skipped.append(
                        SkippedCrateTrack(crate_path, 'the file could not be read')
                    )
                    continue

                song.path = p
                # The client's cues win when it sent any, and the server's copy of
                # the file is not even read then. The two are not equivalent: the
                # file here is whatever was uploaded, so a track subbox already had
                # carries the cues it had *at upload time* and none of the ones the
                # user has set in Serato since. The client is reading the file the
                # user is actually cueing.
                cues = None
                if identity.cues is None:
                    cues = self._read_cues_from_library_copy(p, song)

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
                        serato_hot_cues=cues,
                        client_cues=identity.cues,
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
        identities: Optional[List[SeratoTrackIdentity]] = None,
    ) -> tuple[List[SubBoxPlaylist], CrateImportReport]:
        """
        From the serato crates, create the internal Playlist datastructure.

        ``identities`` is the client's manifest exactly as it arrived: each crate
        entry's stored path, the subbox_id the client read off that local file,
        and the cues it read with it -- see pymix.model.serato_import. Indexed by
        path here rather than by the caller, so there is one place that decides
        what happens when a path appears twice (the last one wins, which is the
        same track either way).
        """
        by_path = {identity.crate_path: identity for identity in (identities or [])}
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
            self._build_subbox_playlists(user, top_level_crate, [], subbox_playlists, by_path, report)

        report.playlists_built = len(subbox_playlists)
        if not subbox_playlists:
            raise ValueError(
                f'none of the {report.total} tracks in your {report.crates_parsed} crates are in '
                f'your subbox library, so there was nothing to build playlists from.'
            )
        return subbox_playlists, report
