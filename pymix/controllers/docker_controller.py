import logging
from pathlib import Path
from typing import List, Optional
from python_on_whales import docker

from pyrekordbox import RekordboxXml
from pyrekordbox.xml import Node

from pymix.factories.rekordbox_xml_factory import RekordboxXMLFactory
from pymix.handlers.filebrowser_file_handler import FileBrowserFileHandler
from pymix.handlers.rb_backup_file_handler import RBBackupFileHandler
from pymix.model.subboxplaylist import SubBoxPlaylist
from pymix.model.subboxtrack import SubBoxTrack
from pymix.orchestrators.rekordbox_xml_orchestrator import RekordboxXMLOrchestrator
from pymix.orchestrators.subsonic_orchestrator import SubsonicOrchestrator

logger = logging.getLogger(__name__)


class DockerController:

    def get_number_of_imported_beets_tracks(self, user: str) -> int:
        """
        # gets the number of audio files present in beets processed /music dir.
        """

        my_container = docker.container.inspect(f"beets{user}")
        # can set to interactive with tty to pipe docker stdin input/output to terminal for user feedback.
        # beets config set to quiet mode and fallback of 'asis'. If user needs to correct later, they will have to
        # specify a musicbrainz id and re import with a specific query. This will need a separate API to be implemented.
        result = docker.execute(my_container, ['find', '/music', '-name', '*.*'])
        print(result)
        return len(result)
