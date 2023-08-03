import logging
from pathlib import Path
from typing import List, Optional

from pyrekordbox import RekordboxXml
from pyrekordbox.xml import Node

from pymix.factories.rekordbox_xml_factory import RekordboxXMLFactory
from pymix.model.subboxplaylist import SubBoxPlaylist
from pymix.model.subboxtrack import SubBoxTrack
from pymix.orchestrators.rekordbox_xml_orchestrator import RekordboxXMLOrchestrator
from pymix.orchestrators.subsonic_orchestrator import SubsonicOrchestrator

logger = logging.getLogger(__name__)


class RekordboxXMLController:
    def __init__(self, subsonic_orchestrator: SubsonicOrchestrator, rekordbox_xml_orchestrator: RekordboxXMLOrchestrator):
        self._subsonic_orchestrator = subsonic_orchestrator
        self._rekordbox_xml_orchestrator = rekordbox_xml_orchestrator


    def _create_rekordbox_xml_playlist(self, subsonic_playlist: SubBoxPlaylist):
        """
        From the playlist given, create the rekordbox folders and playlists.
        Add the tracks to the playlist.
        :param subsonic_playlist:
        :return:
        """
        playlist = self._rekordbox_xml_orchestrator.create_rekordbox_xml_playlist(subsonic_playlist.name)
        for track in subsonic_playlist.tracks:
            self._rekordbox_xml_orchestrator.add_track_to_rekordbox_playlist(track, playlist)

    async def create_rekordbox_xml_from_subsonic_playlists(self, xml_path: Optional[Path], xml_output_path: Path):
        # todo this could be made a context manager to create, update then save the xml
        self._rekordbox_xml_orchestrator.create_xml(xml_path)

        subsonic_playlists = await self._subsonic_orchestrator.get_subsonic_playlists()
        subsonic_tracks = await self._subsonic_orchestrator.get_subsonic_tracks()

        rekordbox_tracks = self._rekordbox_xml_orchestrator.get_all_xml_tracks()

        # If a track in the subsonic set is already present in rekordbox then must remove it before its playlist can be
        # updated. Need the rekordbox TrackID to do this. Therefore, for those subsonic tracks that are already in
        # rekordbox, take the TrackID from the rekordbox set so they can be dealt with.
        for subsonic_track in subsonic_tracks:
            for rekordbox_track in rekordbox_tracks:
                if subsonic_track == rekordbox_track:
                    logger.info(f"found subsonic track {subsonic_track} in rekordbox. Setting track id to {rekordbox_track.track_id}")
                    subsonic_track.track_id = rekordbox_track.track_id

        # Given the Playlist data from Subsonic create the playlist directory structure in Rekordbox.
        for subsonic_playlist in subsonic_playlists:
            self._create_rekordbox_xml_playlist(subsonic_playlist)

        # todo remove any playlists that have no tracks
        self._rekordbox_xml_orchestrator.save_xml(xml_output_path)

    async def create_subsonic_playlists_from_xml(self, xml_path: Path, audio_files_to_import: Path):
        # 1. invoke beets import on the audio files to import
        # 2. resolve import issues
        # 3. beets should import in to the directory navidrome is working off. Once above is complete, check tracks are
        # in navidromes view by using the 'query'/'getSong' apis for each track.
        # 4. create internal subbox playlist and tracks as below
        self._rekordbox_xml_orchestrator.create_xml(xml_path)
        rekordbox_xml_playlists = self._rekordbox_xml_orchestrator.get_all_xml_playlists()
        subbox_playlists = self._rekordbox_xml_orchestrator.get_subbox_playlists_from_rekordbox_xml_playlists(rekordbox_xml_playlists)
        print(subbox_playlists)
        # 5. given the subbox info, create the playlists in navidrome using subsonic api
        # 6. match the tracks in the subbox info against the tracks found from the query in 3. move the matched tracks
        # into the appropriate playlists.


    async def get_healthcheck(self) -> dict:
        return {
            'is_healthy': True
        }
