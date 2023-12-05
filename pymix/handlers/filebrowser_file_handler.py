import logging
from contextlib import contextmanager

import mimetypes
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


class FileBrowserFileHandler:
    def __init__(
            self,
            zip_name: str,
            serving_music_path_base: str,
            filebrowser_data_path: str,
            beets_data_path: str
    ):
        self._zip_name = zip_name
        self._serving_music_path_base = serving_music_path_base.rstrip('/')
        self._filebrowser_data_path = filebrowser_data_path
        self._beets_data_path = beets_data_path
        self._mimetypes = mimetypes.init()

    def get_xml_output_path(self, username: str) -> Path:
        src_path = Path(
            self._filebrowser_data_path.format(user=username)
        )
        return src_path / 'subbox_rb_export.xml'

    def get_xml_audio_path(self, user: str) -> tuple[Path, Path]:
        src_path = Path(
            self._filebrowser_data_path.format(user=user)
        )
        xml_path = None
        audio_path = None
        for f in src_path.iterdir():
            if f.is_file():
                mimestart = mimetypes.guess_type(str(f))[0]
                if mimestart:
                    mimecategory = mimestart.split('/')[1]
                    if mimecategory == 'xml':
                        xml_path = f
            elif f.is_dir():
                if f.name == 'rekordbox_bak':
                    audio_path = f
            if audio_path and xml_path:
                break
        assert xml_path
        assert audio_path
        return xml_path, audio_path

    def get_number_of_tracks_for_import(self, user: str) -> int:
        src_path = Path(
            self._filebrowser_data_path.format(user=user)
        )
        n_files = 0
        # tradeo off here between speed and accuracy. Proper detection of audio file based on content is slow.
        # This detects if audio based on file extension (since the upload could contain other types of files).
        # TODO: must do virus detection in a filebrowser pre hook and proper pre analysis of files.
        for f in src_path.rglob('*'):
            if f.is_file():
                mimestart = mimetypes.guess_type(str(f))[0]
                if mimestart:
                    mimecategory = mimestart.split('/')[0]
                    if mimecategory == 'audio':
                        n_files += 1
        return n_files

    def export_subsonic_music(self, username: str):
        src_dir = self._serving_music_path_base.format(user=username)
        dst_dir = Path(self._filebrowser_data_path.format(user=username)) / self._zip_name
        output_path = str(dst_dir)
        shutil.make_archive(output_path, 'zip', src_dir)

    def stage_for_import(self, username: str):
        """
        copy files from filebrowser to beets input data path.
        """

        src_dir = self._filebrowser_data_path.format(user=username)
        dest_dir = self._beets_data_path.format(user=username)
        logger.info(f'staging for import. Copy from {src_dir} to {dest_dir}')
        shutil.copytree(src_dir, dest_dir, dirs_exist_ok=True)

    def remove_fb_data_path(self, username):
        logger.info(f'removing contents of {self._filebrowser_data_path.format(user=username)}')
        for filepath in Path(self._filebrowser_data_path.format(user=username)).iterdir():
            if filepath.is_dir():
                shutil.rmtree(filepath)
            else:
                filepath.unlink()
