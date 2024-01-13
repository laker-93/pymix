import logging
from pathlib import Path
from typing import List

import anyio
from python_on_whales import docker

from pymix.handlers.filebrowser_file_handler import FileBrowserFileHandler
from pymix.handlers.serato_backup_file_handler import SeratoBackupFileHandler
from pymix.model.subboxplaylist import SubBoxPlaylist
from pymix.orchestrators.serato_crate_orchestrator import SeratoCrateOrchestrator
from pymix.orchestrators.subsonic_orchestrator import SubsonicOrchestrator

logger = logging.getLogger(__name__)


class SeratoController:
    def __init__(
        self,
        subsonic_orchestrator: SubsonicOrchestrator,
        serato_crate_orchestrator: SeratoCrateOrchestrator,
        serato_backup_file_handler: SeratoBackupFileHandler,
        file_browser_file_handler: FileBrowserFileHandler,
    ):
        self._subsonic_orchestrator = subsonic_orchestrator
        self._serato_crate_orchestrator = serato_crate_orchestrator
        self._serato_backup_file_handler = serato_backup_file_handler
        self._file_browser_file_handler = file_browser_file_handler


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
        async for tracks in self._subsonic_orchestrator._subsonic_client.get_all_tracks(user, 400):
            for track in tracks:
                self._serato_crate_orchestrator.add_track_to_crate(
                    user_root,
                    track,
                    default_crate
                )
            await asyncio.sleep(2)

        # todo remove any playlists that have no tracks
        self._serato_crate_orchestrator.save(default_crate, output_path)

    # todo this function should be part of the beets client or beets controller class and removed from here and rekordbox_xml_controller.py
    def _import_to_beets(self, username: str, audio_file_zip: Path):
        """
        Import into beets in quiet mode. Any exceptions will interrupt the process.
        beets should import in to the directory navidrome is working off.
        Users can use APIs after import to correct any mistakes from the beets quiet import.
        """
        self._serato_backup_file_handler.stage_for_import(username, audio_file_zip)
        # 1. invoke beets import on the audio files to import

        # can set to interactive with tty to pipe docker stdin input/output to terminal for user feedback.
        # beets config set to quiet mode and fallback of 'asis'. If user needs to correct later, they will have to
        # specify a musicbrainz id and re import with a specific query. This will need a separate API to be implemented.
        logger.info(f'starting beets import for {username}')
        result = docker.execute(f"beets{username}", ['beet', 'import', '-q', '/downloads'])
        logger.info(f'finished beets import for {username}')
        print(result)
        # 9. on success, remove the directory of the beets import
        logger.info(f'starting post import clean up for {username}')
        #self._file_browser_file_handler.remove_fb_data_path(username)
        self._serato_backup_file_handler.clean_up_beets_import_tree(username)
        logger.info(f'finished post import clean up for {username}')

    async def create_subsonic_playlists_from_crates(self, user: dict, serato_crate_path: Path, audio_files_to_import: Path):
        username = user['username']

        await anyio.to_thread.run_sync(self._import_to_beets, username, audio_files_to_import)
        # must trigger a navidrome scan so the tracks will be queryable when creating and moving in to playlists in the
        # next step
        await self._subsonic_orchestrator.scan(user)
        await anyio.sleep(2)
        await self.create_subsonic_playlists(user, serato_crate_path)

    async def create_subsonic_playlists(self, user: dict, serato_crate_path: Path):

        # 4. create internal subbox playlist and tracks as below
        subbox_playlists = self._serato_crate_orchestrator.get_subbox_playlists_from_crates(serato_crate_path)
        # 5. given the subbox info, create the playlists in navidrome using subsonic api
        # 6. get the tracks from navidrome by using the 'query' api for each track.
        # this sets the subsonic id found from querying navidrome. This can then be used to create the playlist and place
        # the track in the playlist
        res = await self._subsonic_orchestrator.update_tracks_with_subid(user, subbox_playlists)
        # 8. create the playlists and set the rating of the track in navidrome from the rating taken from xml
        await self._subsonic_orchestrator.create_playlists_and_set_rating(user, subbox_playlists)
