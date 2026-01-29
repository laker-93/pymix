import logging
from pathlib import Path
from typing import List, Optional

import anyio
from pyserato.model.hot_cue_type import HotCueType
from python_on_whales import docker

from pymix.controllers.db_controller import DbController
from pymix.controllers.rekordbox_xml_controller import RekordboxXMLController
from pymix.handlers.filebrowser_file_handler import FileBrowserFileHandler
from pymix.handlers.rb_backup_file_handler import RBBackupFileHandler
from pymix.handlers.serato_backup_file_handler import SeratoBackupFileHandler
from pymix.model.subboxplaylist import SubBoxPlaylist
from pymix.orchestrators.serato_crate_orchestrator import SeratoCrateOrchestrator
from pymix.orchestrators.subsonic_orchestrator import SubsonicOrchestrator
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
        serving_music_path_base: str
    ):
        self._subsonic_orchestrator = subsonic_orchestrator
        self._serato_crate_orchestrator = serato_crate_orchestrator
        self._serato_backup_file_handler = serato_backup_file_handler
        self._file_browser_file_handler = file_browser_file_handler
        self._rb_backup_file_handler = rb_backup_file_handler
        self._rb_xml_controller = rb_xml_controller
        self._db_controller = db_controller
        self._serving_music_path_base = serving_music_path_base


    def _create_serato_crates(self, user_root: str, subsonic_playlist: SubBoxPlaylist, output_path: Path):
        """
        From the playlist given, create the rekordbox folders and playlists.
        Add the tracks to the playlist.
        :param subsonic_playlist:
        :return:
        """
        crate = self._serato_crate_orchestrator.create_crate(subsonic_playlist.name)
        for track in subsonic_playlist.tracks:
            self._serato_crate_orchestrator.add_track_to_crate(user_root, track, crate)
        self._serato_crate_orchestrator.save(crate, output_path)

    async def create_crates_from_subsonic_playlists(self, user_root: str, user: dict, output_path: Path):
        subsonic_playlists = await self._subsonic_orchestrator.get_subsonic_playlists(user)
        if not subsonic_playlists:
            logger.info(f'no subsonic playlists found for user')
        else:
            # sort the playlists by name so duplicate folders of the same name are not created
            subsonic_playlists.sort(key=lambda playlist: playlist.name)
            # Given the Playlist data from Subsonic create the playlist directory structure in Rekordbox.
            for subsonic_playlist in subsonic_playlists:
                # can do something here along the lines of keeping the root node
                self._create_serato_crates(user_root, subsonic_playlist, output_path)
        # add subsonic tracks that do not belong to a playlist to a default playlist.
        default_crate = self._serato_crate_orchestrator.create_crate('NOPLAYLIST')
        # suppress the exception that would be raised due to attempting to add a track that is already present.
        import asyncio
        # todo figure this out - seem to need to pause to avoid getting disconnected from server
        await asyncio.sleep(2)
        async for tracks in self._subsonic_orchestrator._subsonic_client.get_all_tracks(user, 200):
            for track in tracks:
                self._serato_crate_orchestrator.add_track_to_crate(
                    user_root,
                    track,
                    default_crate
                )
                # todo - handle meta data such as cues and ratings
            await asyncio.sleep(2)

        # todo remove any playlists that have no tracks
        self._serato_crate_orchestrator.save(default_crate, output_path)

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
        beets_command = f"beet import --group-albums --set user={username} -q /downloads"
        try:
            log_iter = docker.execute(f"beets{username}", beets_command.split(), stream=True)
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
            src_dir = self._serving_music_path_base.format(user=username)
            make_readable(Path(src_dir))
            # todo - get the duplicates before the import and before tagging the new duplicates, untag the old ones and do so atomically.
            # todo move this logic out of the rb xml controller
            self._rb_xml_controller._get_duplicates(username, False)
            self._rb_xml_controller._map_subbox_id_beet_id(username, False)

    async def create_subsonic_playlists_from_crates(self, user: dict, serato_crate_path: Path, zip_path: Optional[Path], audio_path: Optional[Path]):
        username = user['username']

        if zip_path or audio_path:
            await anyio.to_thread.run_sync(self._import_to_beets, username, zip_path, audio_path)
        # must trigger a navidrome scan so the tracks will be queryable when creating and moving in to playlists in the
        # next step
        await self._subsonic_orchestrator.scan(user)
        await anyio.sleep(2)
        await self._set_data_from_crates(user, serato_crate_path)
        # the fb path is removed here as it's needed for processing the .crate files so can't be removed in
        # import_to_beets stage.
        self._file_browser_file_handler.remove_fb_data_path(username)

    async def _set_data_from_crates(self, user: dict, serato_crate_path: Path):
        subbox_playlists = await self._create_subsonic_playlists(user, serato_crate_path)
        await self._set_metadata(user, subbox_playlists)

    async def _create_subsonic_playlists(self, user: dict, serato_crate_path: Path) -> List[SubBoxPlaylist]:

        # 4. create internal subbox playlist and tracks as below
        subbox_playlists = self._serato_crate_orchestrator.get_subbox_playlists_from_crates(user, serato_crate_path)
        # 5. given the subbox info, create the playlists in navidrome using subsonic api
        # 6. get the tracks from navidrome by using the 'query' api for each track.
        # this sets the subsonic id found from querying navidrome. This can then be used to create the playlist and place
        # the track in the playlist
        await self._subsonic_orchestrator.update_tracks_with_subid(user, subbox_playlists)
        # 8. create the playlists
        await self._subsonic_orchestrator.create_playlists(user, subbox_playlists)
        return subbox_playlists

    async def _set_metadata(self, user, subbox_playlists: List[SubBoxPlaylist]):
        tracks = []
        for playlist in subbox_playlists:
            if playlist.tracks:
                tracks.extend(playlist.tracks)
        rated_tracks = list(filter(lambda t: t.rating > 0, tracks))
        # set the rating of the track in navidrome from the rating taken from track meta
        await self._subsonic_orchestrator.set_ratings(user, rated_tracks)
        for track in tracks:
            if track.serato_hot_cues:
                cues = list(filter(lambda m: m.type == HotCueType.CUE, track.serato_hot_cues))
                loops = list(filter(lambda m: m.type == HotCueType.LOOP, track.serato_hot_cues))
                # todo extract colors of cues
                assert track.subbox_id is not None, f"subbox id tag not present on {track}"
                self._db_controller.update_metadata(
                    username=user['username'],
                    subbox_id=track.subbox_id,
                    cuedata={
                        "cues": [
                            {
                                "index": cue.index,
                                "position": int(cue.start * 1000),
                                "name": cue.name
                                # "color": cue.color todo
                            } for i, cue in enumerate(cues)
                        ],
                        "loops": [
                            {
                                "index": cue.index,
                                "start": int(cue.start * 1000),
                                "end": int(cue.end * 1000),
                                "active": False
                                # "color": cue.color todo
                            } for i, cue in enumerate(loops)
                        ],
                    },
                    source_app="serato",
                    change_type="upload"
                )

