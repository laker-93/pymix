import logging
import zipfile

import shutil
from pathlib import Path


logger = logging.getLogger(__name__)


class SeratoBackupFileHandler:
    def __init__(
            self,
            beets_data_path: str
    ):
        self._beets_data_path = beets_data_path

    def clean_up_beets_import_tree(self, username: str):
        """
        After successful import in to beets, can remove the source tree that was imported.
        """
        beets_data_path = self._beets_data_path.format(user=username)
        logger.info(f'removing contents of {beets_data_path}')
        for filepath in Path(beets_data_path).iterdir():
            if filepath.is_dir():
                shutil.rmtree(filepath)
            else:
                filepath.unlink()

    def stage_for_import(self, username: str, audio_files_zip: Path) -> None:
        """
        Unzip the audio files to the  beets docker shared directory that is used for import in to beets.
        """
        beets_data_path = self._beets_data_path.format(user=username)
        with zipfile.ZipFile(audio_files_zip, 'r') as zip_ref:
            zip_ref.extractall(beets_data_path)

