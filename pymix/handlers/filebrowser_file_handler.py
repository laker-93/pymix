import logging
from contextlib import contextmanager

import music_tag
import os
import shutil
from pathlib import Path

from pyrekordbox.xml import Track

from pymix.orchestrators.rekordbox_xml_orchestrator import RekordboxXMLOrchestrator

logger = logging.getLogger(__name__)


class FileBrowserFileHandler:
    def __init__(
            self,
            filebrowser_data_path: str,
            beets_data_path: str
    ):
        self._filebrowser_data_path = filebrowser_data_path
        self._beets_data_path = beets_data_path

    @contextmanager
    def stage_for_import(self):
        """
        copy files from filebrowser to beets input data path.
        Delete files on success.
        """

        shutil.copytree(self._filebrowser_data_path, self._beets_data_path, dirs_exist_ok=True)
        yield
        shutil.rmtree(self._filebrowser_data_path)

        for filepath in Path(self._beets_data_path).iterdir():
            if filepath.is_dir():
                shutil.rmtree(filepath)
            else:
                filepath.unlink()
