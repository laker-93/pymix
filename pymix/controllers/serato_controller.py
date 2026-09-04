import logging
from pathlib import Path
from typing import Dict, List, Optional

import anyio
from pyserato.model.hot_cue_type import HotCueType

from pymix.clients.beets_exec import BeetsExec
from pymix.controllers.db_controller import DbController
from pymix.controllers.rekordbox_xml_controller import RekordboxXMLController
from pymix.handlers.filebrowser_file_handler import FileBrowserFileHandler
from pymix.handlers.rb_backup_file_handler import RBBackupFileHandler
from pymix.handlers.serato_backup_file_handler import SeratoBackupFileHandler
from pymix.model import beatgrid
from pymix.model.serato_cue import SeratoCue, from_cuedata, to_cuedata
from pymix.model.serato_export import (
    SeratoExportCrate,
    SeratoExportResponse,
    SeratoExportTrack,
)
from pymix.model.serato_import import CrateImportReport
from pymix.model.subboxplaylist import SubBoxPlaylist
from pymix.model.subboxtrack import SubBoxTrack
from pymix.orchestrators.serato_crate_orchestrator import SeratoCrateOrchestrator
from pymix.orchestrators.subsonic_orchestrator import SubsonicOrchestrator
from pymix.services.wishlist_reconcile_service import WishlistReconcileService
from pymix.utils.make_readable import make_readable

logger = logging.getLogger(__name__)


class SeratoController:
    def __init__(
        self,
        subsonic_orchestrator: SubsonicOrchestrator,
        serato_crate_orchestrator: SeratoCrateOrchestrator,
        serato_backup_file_handler: SeratoBackupFileHandler,
        file_browser_file_handler: FileBrowserFileHandler,
        rb_backup_file_handler: RBBackupFileHandler,
        rb_xml_controller: RekordboxXMLController,
        db_controller: DbController,
        wishlist_reconcile_service: WishlistReconcileService,
        serving_music_path_base: str,
        beets_exec: BeetsExec,
    ):
        self._subsonic_orchestrator = subsonic_orchestrator
        self._serato_crate_orchestrator = serato_crate_orchestrator
        self._serato_backup_file_handler = serato_backup_file_handler
        self._file_browser_file_handler = file_browser_file_handler
        self._rb_backup_file_handler = rb_backup_file_handler
        self._rb_xml_controller = rb_xml_controller
        self._db_controller = db_controller
        self._wishlist_reconcile_service = wishlist_reconcile_service
        self._serving_music_path_base = serving_music_path_base
        self._beets_exec = beets_exec


    def _relative_path_in_export(self, username: str, track: SubBoxTrack) -> Optional[str]:
        """Where this track will sit inside the download zip, minus the music/ prefix.

        The one value the client needs to find the file it just downloaded, so it
        is taken from the same music root the zip's own entry names are built
        from rather than re-derived from the Navidrome path. A track that isn't
        under that root can't be in the zip either, so there is nothing useful to
        return for it.
        """
        if not track.pymix_path:
            return None
        root = self._file_browser_file_handler.get_user_music_root(username)
        try:
            return str(Path(track.pymix_path).relative_to(root))
        except ValueError:
            logger.warning(
                'export: track path %s is not under the music root %s for user %s',
                track.pymix_path, root, username,
            )
            return None

    async def get_export_structure(
        self, user: dict, playlist_ids: Optional[List[str]] = None
    ) -> SeratoExportResponse:
        """The user's playlists as crates-to-be, for the client to write.

        Everything here is something only the server knows: which playlists
        exist, what is in them, and the cues subbox is holding. Where the files
        will land, what the crate files should be called and what goes in them
        is the client's half, because the client is the side with the filesystem.
        """
        username = user['username']
        id_set = set(playlist_ids) if playlist_ids else None
        subsonic_playlists = await self._subsonic_orchestrator.get_subsonic_playlists(user, id_set)
        if not subsonic_playlists:
            logger.info(f'no subsonic playlists found for user {username}')
            return SeratoExportResponse(success=True, reason='no playlists to export')

        # Sorted so a parent crate is written before its children, same reason the
        # Rekordbox export sorts: two playlists under one folder must not each
        # create their own copy of it.
        subsonic_playlists.sort(key=lambda playlist: playlist.name)

        # The stored components are the lossless form; the display name is a
        # ' / ' join of them and can't be split back apart safely (a playlist
        # whose own name contains ' / ' would split into the wrong tree).
        path_rows = self._db_controller.get_playlist_paths(username)
        path_map = {row['display_name']: row['path_components'] for row in path_rows}

        subbox_ids = [
            track.subbox_id
            for playlist in subsonic_playlists
            for track in (playlist.tracks or [])
            if track.subbox_id
        ]
        cuedata_by_id = self._db_controller.get_cuedata_by_subbox_id(username, subbox_ids)

        crates: List[SeratoExportCrate] = []
        n_tracks = 0
        for playlist in subsonic_playlists:
            components = path_map.get(playlist.name) or playlist.name.split(' / ')
            tracks: List[SeratoExportTrack] = []
            for track in playlist.tracks or []:
                relative_path = self._relative_path_in_export(username, track)
                if relative_path is None:
                    continue
                stored_grid = beatgrid.from_cuedata(cuedata_by_id.get(track.subbox_id))
                tracks.append(
                    SeratoExportTrack(
                        relative_path=relative_path,
                        title=track.name,
                        artist=track.artist,
                        album=track.album,
                        rating=track.rating or 0,
                        subbox_id=track.subbox_id,
                        cues=from_cuedata(cuedata_by_id.get(track.subbox_id)),
                        beatgrid=beatgrid.to_serato_anchors(stored_grid),
                        beatgrid_notes=beatgrid.lossy_notes(stored_grid),
                    )
                )
            if not tracks:
                # An empty crate is worse than no crate: it looks to the user like
                # subbox lost the tracks. Same call the import makes in the other
                # direction (see SeratoCrateOrchestrator._build_subbox_playlists).
                logger.info(f'export: playlist {playlist.name} has no exportable tracks, skipping')
                continue
            n_tracks += len(tracks)
            crates.append(
                SeratoExportCrate(
                    path_components=components,
                    display_name=playlist.name,
                    tracks=tracks,
                )
            )

        return SeratoExportResponse(
            success=True,
            crates=crates,
            n_crates=len(crates),
            n_tracks=n_tracks,
        )

    # todo this function should be part of the beets client or beets controller class and removed from here and rekordbox_xml_controller.py
    def _import_to_beets(self, username: str, zip_path: Optional[Path], audio_path: Optional[Path]):
        """
        Import into beets in quiet mode. Any exceptions will interrupt the process.
        beets should import in to the directory navidrome is working off.
        Users can use APIs after import to correct any mistakes from the beets quiet import.
        """
        if zip_path:
            self._serato_backup_file_handler.stage_for_import(username, zip_path)
        if audio_path:
            # todo: move from rb handler as logic is generic to serato and rb
            self._rb_backup_file_handler.stage_for_import(username, audio_path)
        # 1. invoke beets import on the audio files to import

        # can set to interactive with tty to pipe docker stdin input/output to terminal for user feedback.
        # beets config set to quiet mode and fallback of 'asis'. If user needs to correct later, they will have to
        # specify a musicbrainz id and re import with a specific query. This will need a separate API to be implemented.
        logger.info(f'starting beets import for {username}')
        beets_command = f"beet import -A --group-albums --set user={username} --set automatch_state=pending /downloads"
        # One lock for the whole job (import -> duplicates -> subbox_id map),
        # shared with RekordboxXMLController via the same injected BeetsExec
        # singleton so a serato and a rekordbox import for the same user's
        # beets container can't interleave either (#73).
        with self._beets_exec.write_lock(f"beets{username}"):
            try:
                log_iter = self._beets_exec.execute(f"beets{username}", beets_command, stream=True)
                for log_type, log in log_iter:
                    logger.info(f'{log_type}: {log.decode()}')
            except Exception:
                logger.exception('beets import failed')
                raise
            else:
                logger.info(f'finished beets import for {username}')
                # 9. on success, remove the directory of the beets import
                logger.info(f'starting post import clean up for {username}')
                self._serato_backup_file_handler.clean_up_beets_import_tree(username)
            finally:
                # we want to handle the meta data regardless as we could have had some files that were successfully imported
                # in those cases, we want to handle the meta data so they are skipped on next import attempt
                # set permissions so navidrome can read - todo: remove this by running pymix as non root
                #src_dir = self._serving_music_path_base.format(user=username)
                #make_readable(Path(src_dir))
                # todo - get the duplicates before the import and before tagging the new duplicates, untag the old ones and do so atomically.
                # todo move this logic out of the rb xml controller
                self._rb_xml_controller._get_duplicates(username, False)
                self._rb_xml_controller._map_subbox_id_beet_id(username, False)

    async def create_subsonic_playlists_from_crates(
        self,
        user: dict,
        serato_crate_path: Path,
        zip_path: Optional[Path],
        audio_path: Optional[Path],
        identities: Optional[Dict[str, str]] = None,
    ) -> CrateImportReport:
        username = user['username']

        if zip_path or audio_path:
            await anyio.to_thread.run_sync(self._import_to_beets, username, zip_path, audio_path)
        # must trigger a navidrome scan so the tracks will be queryable when creating and moving in to playlists in the
        # next step
        await self._subsonic_orchestrator.scan(user)
        await anyio.sleep(2)
        report = await self._set_data_from_crates(user, serato_crate_path, identities)
        # Resolve any open wishlist items whose track is now in the user's Navidrome.
        try:
            await self._wishlist_reconcile_service.reconcile_user(user)
        except Exception:
            logger.exception(f"wishlist reconcile after serato import failed for {username}")
        # the fb path is removed here as it's needed for processing the .crate files so can't be removed in
        # import_to_beets stage. Also we only want to remove data in fb once import is successful to avoid
        # unnecessarily having to reupload data from the client after a beets import failure
        self._file_browser_file_handler.remove_fb_data_path(username)
        return report

    async def _set_data_from_crates(
        self,
        user: dict,
        serato_crate_path: Path,
        identities: Optional[Dict[str, str]] = None,
    ) -> CrateImportReport:
        subbox_playlists, report = await self._create_subsonic_playlists(user, serato_crate_path, identities)
        await self._set_metadata(user, subbox_playlists)
        return report

    async def _create_subsonic_playlists(
        self,
        user: dict,
        serato_crate_path: Path,
        identities: Optional[Dict[str, str]] = None,
    ) -> tuple[List[SubBoxPlaylist], CrateImportReport]:

        # 4. create internal subbox playlist and tracks as below
        subbox_playlists, report = self._serato_crate_orchestrator.get_subbox_playlists_from_crates(
            user, serato_crate_path, identities
        )
        # 5. given the subbox info, create the playlists in navidrome using subsonic api
        # 6. get the tracks from navidrome by using the 'query' api for each track.
        # this sets the subsonic id found from querying navidrome. This can then be used to create the playlist and place
        # the track in the playlist
        await self._subsonic_orchestrator.update_tracks_with_subid(user, subbox_playlists)
        # 8. create the playlists
        await self._subsonic_orchestrator.create_playlists(user, subbox_playlists)
        return subbox_playlists, report

    @staticmethod
    def _cuedata_for(track: SubBoxTrack, stored: Optional[Dict] = None) -> Optional[Dict]:
        """The blob to store for this track, or None if there is nothing to store.

        The client's reading of the user's own file wins over pyserato's reading
        of the server's copy, for cues and for the grid independently — see
        SeratoTrackIdentity. An empty reading gives None rather than an empty
        blob, so a track with no cues leaves whatever subbox already holds alone
        instead of overwriting it with nothing.

        Built *onto* `stored` rather than from scratch, because update_metadata
        replaces the row's blob wholesale. Without this, "an empty reading does
        not clear what's stored" would only be true of a track subbox had never
        seen: a Serato import of a track that had been through the Rekordbox one
        would write a cues-only blob over the top and take the `bpm` and any
        stored grid with it. That was already true of the `bpm` before the grid
        existed; the grid makes it much easier to hit, since the two importers
        now write overlapping keys.
        """
        cuedata = dict(stored or {})
        stored_anything = False

        cues = None
        if track.client_cues is not None:
            cues = to_cuedata(track.client_cues)
        elif track.serato_hot_cues:
            # todo extract colors of cues
            cues = to_cuedata([
                SeratoCue(
                    type='loop' if cue.type == HotCueType.LOOP else 'cue',
                    index=cue.index,
                    name=cue.name,
                    start_ms=int(cue.start),
                    end_ms=int(cue.end) if cue.end is not None else None,
                )
                for cue in track.serato_hot_cues
                if cue.type in (HotCueType.CUE, HotCueType.LOOP)
            ])
        if cues and (cues['cues'] or cues['loops']):
            cuedata.update(cues)
            stored_anything = True

        # A track can carry a grid and no cues, so this is asked separately
        # rather than inside the cue branches.
        grid = track.client_beatgrid if track.client_beatgrid is not None else track.beatgrid
        grid_blob = beatgrid.to_cuedata(grid or [])
        if grid_blob:
            cuedata['beatgrid'] = grid_blob
            stored_anything = True

        return cuedata if stored_anything else None

    async def _set_metadata(self, user, subbox_playlists: List[SubBoxPlaylist]):
        tracks = []
        for playlist in subbox_playlists:
            if playlist.tracks:
                tracks.extend(playlist.tracks)
        rated_tracks = list(filter(lambda t: t.rating > 0, tracks))
        # set the rating of the track in navidrome from the rating taken from track meta
        await self._subsonic_orchestrator.set_ratings(user, rated_tracks)
        # One query for the lot: _cuedata_for merges onto what is already stored,
        # and asking per track is a round trip per track (see
        # DbController.get_cuedata_by_subbox_id).
        stored_by_id = self._db_controller.get_cuedata_by_subbox_id(
            user['username'], [t.subbox_id for t in tracks if t.subbox_id],
        )
        for track in tracks:
            cuedata = self._cuedata_for(track, stored_by_id.get(track.subbox_id))
            if cuedata:
                assert track.subbox_id is not None, f"subbox id tag not present on {track}"
                self._db_controller.update_metadata(
                    username=user['username'],
                    subbox_id=track.subbox_id,
                    cuedata=cuedata,
                    source_app="serato",
                    change_type="upload"
                )
