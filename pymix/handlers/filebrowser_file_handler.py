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

    def get_number_of_tracks_for_import(self, user: str) -> int:
        src_path = Path(
            self._filebrowser_data_path.format(user=user)
        )
        n_files = sum(1 for f in src_path.rglob('*') if f.is_file())
        return n_files


    @contextmanager
    def stage_for_import(self, user: str):
        """
        copy files from filebrowser to beets input data path.
        Delete files on success.
        """

        src_dir = self._filebrowser_data_path.format(user=user)
        dest_dir = self._beets_data_path.format(user=user)
        logger.info(f'staging for import. Copy from {src_dir} to {dest_dir}')
        shutil.copytree(src_dir, dest_dir, dirs_exist_ok=True)
        yield
        logger.info(f'removing contents of {src_dir}')
        for filepath in Path(src_dir).iterdir():
            if filepath.is_dir():
                shutil.rmtree(filepath)
            else:
                filepath.unlink()

        for filepath in Path(dest_dir).iterdir():
            if filepath.is_dir():
                shutil.rmtree(filepath)
            else:
                filepath.unlink()
