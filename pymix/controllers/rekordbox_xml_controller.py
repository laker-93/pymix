import logging
import anyio
from pathlib import Path
from typing import List, Optional
from python_on_whales import docker

from pymix.handlers.filebrowser_file_handler import FileBrowserFileHandler
from pymix.handlers.rb_backup_file_handler import RBBackupFileHandler
from pymix.model.subboxplaylist import SubBoxPlaylist
from pymix.orchestrators.rekordbox_xml_orchestrator import RekordboxXMLOrchestrator
from pymix.orchestrators.subsonic_orchestrator import SubsonicOrchestrator

logger = logging.getLogger(__name__)


class RekordboxXMLController:
    def __init__(
            self,
            subsonic_orchestrator: SubsonicOrchestrator,
            rekordbox_xml_orchestrator: RekordboxXMLOrchestrator,
            rb_backup_file_handler: RBBackupFileHandler,
            file_browser_file_handler: FileBrowserFileHandler,
            restored_db_output_root: str
    ):
        self._subsonic_orchestrator = subsonic_orchestrator
        self._rekordbox_xml_orchestrator = rekordbox_xml_orchestrator
        self._rb_backup_file_handler = rb_backup_file_handler
        self._file_browser_file_handler = file_browser_file_handler
        self._restored_db_output_root = restored_db_output_root

    def _create_rekordbox_xml_playlist(self, user_root: str, subsonic_playlist: SubBoxPlaylist):
        """
        From the playlist given, create the rekordbox folders and playlists.
        Add the tracks to the playlist.
        :param subsonic_playlist:
        :return:
        """
        playlist = self._rekordbox_xml_orchestrator.create_rekordbox_xml_playlist(subsonic_playlist.name)
        for track in subsonic_playlist.tracks:
            self._rekordbox_xml_orchestrator.add_track_to_rekordbox_playlist(user_root, track, playlist)

    # todo this controller is overloaded; this method has nothing to do with rekordbox xml and should live elsewhere.
    async def consume_from_filebrowser(self, username: str, public: bool) -> str:
        return await anyio.to_thread.run_sync(self._consume_from_filebrowser, username, public)

    def _consume_from_filebrowser(self, username: str, public: bool) -> str:
        """
        # steps:
        # 1. user uploads to filebrowser
        # 2. stage filebrowser/data to beets import
        # 3. do beet import
        """

        self._file_browser_file_handler.stage_for_import(username, public)
        # 1. invoke beets import on the audio files to import
        # can set to interactive with tty to pipe docker stdin input/output to terminal for user feedback.
        # beets config set to quiet mode and fallback of 'asis'. If user needs to correct later, they will have to
        # specify a musicbrainz id and re import with a specific query. This will need a separate API to be implemented.
        container_name = "beets" if public else f"beets{username}"
        #result = docker.execute(container_name, "ls /downloads".split())
        #logger.info(f"beets has following tracks waiting for import {result}")
        # set a custom field of the username that uploaded the track. This allows to query tracks that a username has uploaded.
        beets_command = f"beet import --set user={username} --set public={public} -q /downloads"
        result = docker.execute(container_name, beets_command.split())
        logger.info(f"got result {result} from running beets command {beets_command} on container {container_name}")
        if public:
            # must write the user tag set above to the audio file
            write_result = docker.execute(container_name, "beet write".split())
            logger.info("result from writing beets db to files %s", write_result)

        self._file_browser_file_handler.remove_fb_data_path(username)
        self._rb_backup_file_handler.clean_up_beets_import_tree(username, public)
        return result

    async def create_rekordbox_xml_from_subsonic_playlists(self, user_root: str, user: dict, xml_path: Optional[Path], xml_output_path: Path):
        # todo this could be made a context manager to create, update then save the xml
        self._rekordbox_xml_orchestrator.create_xml(xml_path)

        subsonic_playlists = await self._subsonic_orchestrator.get_subsonic_playlists(user)
        if not subsonic_playlists:
            logger.info(f'no subsonic playlists found for user')
        subsonic_tracks = await self._subsonic_orchestrator.get_subsonic_tracks(user)

        rekordbox_tracks = self._rekordbox_xml_orchestrator.get_all_xml_tracks()

        # If a track in the subsonic set is already present in rekordbox then must remove it before its playlist can be
        # updated. Need the rekordbox TrackID to do this. Therefore, for those subsonic tracks that are already in
        # rekordbox, take the TrackID from the rekordbox set so they can be dealt with.
        for subsonic_track in subsonic_tracks:
            for rekordbox_track in rekordbox_tracks:
                if subsonic_track == rekordbox_track:
                    logger.info(
                        f"found subsonic track {subsonic_track} in rekordbox. Setting track id to {rekordbox_track.track_id}")
                    subsonic_track.track_id = rekordbox_track.track_id

        if subsonic_playlists:
            # sort the playlists by name so duplicate folders of the same name are not created
            subsonic_playlists.sort(key=lambda playlist: playlist.name)
            # Given the Playlist data from Subsonic create the playlist directory structure in Rekordbox.
            for subsonic_playlist in subsonic_playlists:
                self._create_rekordbox_xml_playlist(user_root, subsonic_playlist)
        # add subsonic tracks that do not belong to a playlist to a default playlist.
        default_playlist = self._rekordbox_xml_orchestrator.create_rekordbox_xml_playlist('NOPLAYLIST')
        # suppress the exception that would be raised due to attempting to add a track that is already present.
        import asyncio
        # todo figure this out - seem to need to pause to avoid getting disconnected from server
        await asyncio.sleep(2)
        async for tracks in self._subsonic_orchestrator._subsonic_client.get_all_tracks(user, 400):
            for track in tracks:
                self._rekordbox_xml_orchestrator.add_track_to_rekordbox_playlist(
                    user_root,
                    track,
                    default_playlist,
                    force=False
                )
            await asyncio.sleep(2)

        # todo remove any playlists that have no tracks
        self._rekordbox_xml_orchestrator.save_xml(xml_output_path)

    def _import_to_beets(self, username: str, audio_files_to_import: Path):
        """
        Import into beets in quiet mode. Any exceptions will interrupt the process.
        beets should import in to the directory navidrome is working off.
        Users can use APIs after import to correct any mistakes from the beets quiet import.
        """
        self._rb_backup_file_handler.restore_track_meta_and_stage_for_import(username, audio_files_to_import)
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
        self._file_browser_file_handler.remove_fb_data_path(username)
        # todo - inject public in from router
        self._rb_backup_file_handler.clean_up_beets_import_tree(username, False)
        logger.info(f'finished post import clean up for {username}')

    async def create_subsonic_playlists_from_xml(self, user: dict, xml_path: Path, audio_files_to_import: Path):
        username = user['username']
        self._rekordbox_xml_orchestrator.create_xml(xml_path)

        await anyio.to_thread.run_sync(self._import_to_beets, username, audio_files_to_import)
        # must trigger a navidrome scan so the tracks will be queryable when creating and moving in to playlists in the
        # next step
        await self._subsonic_orchestrator.scan(user)
        await anyio.sleep(2)
        await self.create_subsonic_playlists(user, xml_path)

    async def create_subsonic_playlists(self, user: dict, xml_path: Path):

        # 4. create internal subbox playlist and tracks as below
        rekordbox_xml_playlists = self._rekordbox_xml_orchestrator.get_all_xml_playlists()
        subbox_playlists: List[SubBoxPlaylist] = []
        self._rekordbox_xml_orchestrator.get_subbox_playlists_from_rekordbox_xml_playlists(rekordbox_xml_playlists, '',
                                                                                           subbox_playlists)
        # 5. given the subbox info, create the playlists in navidrome using subsonic api
        # 6. get the tracks from navidrome by using the 'query' api for each track.
        # this sets the subsonic id found from querying navidrome. This can then be used to create the playlist and place
        # the track in the playlist
        res = await self._subsonic_orchestrator.update_tracks_with_subid(user, subbox_playlists)
        # 8. create the playlists and set the rating of the track in navidrome from the rating taken from xml
        await self._subsonic_orchestrator.create_playlists_and_set_rating(user, subbox_playlists)

    async def get_healthcheck(self) -> dict:
        return {
            'is_healthy': True
        }
