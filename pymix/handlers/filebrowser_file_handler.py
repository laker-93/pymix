import datetime
import logging

import mimetypes
import shutil
import zipfile
from pathlib import Path
from zipfile import ZipFile

from tinydb import TinyDB

from pymix.controllers.db_controller import DbController

logger = logging.getLogger(__name__)


class FileBrowserFileHandler:
    def __init__(
            self,
            zip_name: str,
            serving_music_path_base: str,
            filebrowser_data_path: str,
            beets_data_path: str,
            update_job_period_s: int,
    ):
        self._zip_name = zip_name
        self._serving_music_path_base = serving_music_path_base.rstrip('/')
        self._filebrowser_data_path = filebrowser_data_path
        self._beets_data_path = beets_data_path
        self._update_job_period_s = update_job_period_s
        self._mimetypes = mimetypes.init()

    def get_xml_output_path(self, username: str) -> Path:
        src_path = Path(
            self._filebrowser_data_path.format(user=username)
        )
        xml_path = src_path / 'subbox_rb_export.xml'
        # ensure starting from a clean state.
        # multiple exports could pick up the xml from a previous run.
        xml_path.unlink(missing_ok=True)
        return xml_path

    def get_crate_output_path(self, username: str) -> Path:
        src_path = Path(
            self._filebrowser_data_path.format(user=username)
        )
        path = src_path
        return path


    def get_subcrate_audio_path(self, user: str) -> tuple[Path, Path]:
        src_path = Path(
            self._filebrowser_data_path.format(user=user)
        )
        subcrate_path = None
        audio_path = None
        for f in src_path.iterdir():
            if f.name.lower() == 'audio_files.zip':
                audio_path = f
            elif f.name.lower() == 'subcrates.zip':
                subcrate_path = f
            if audio_path and subcrate_path:
                break
        assert subcrate_path
        assert audio_path
        return subcrate_path, audio_path


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
                if f.name.endswith('.zip'):
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
        audio_files_zip = None
        for f in src_path.iterdir():
            if f.is_file() and f.name.endswith('.zip'):
                audio_files_zip = f
                break
        assert audio_files_zip, f"no audio files zip found in {src_path}"
        n_files = 0
        with ZipFile(audio_files_zip) as zip:
            files = zip.namelist()
            for f in files:
                mimestart = mimetypes.guess_type(str(f))[0]
                if mimestart:
                    mimecategory = mimestart.split('/')[0]
                    if mimecategory == 'audio':
                        n_files += 1

        return n_files

    def export_subsonic_music(self, db_path: str, app_env: str, username: str, job_id: str) -> int:
        db_controller = DbController(TinyDB(db_path), app_env)
        src_dir = self._serving_music_path_base.format(user=username)
        dst_dir = Path(self._filebrowser_data_path.format(user=username)) / self._zip_name
        dst_dir = dst_dir.with_suffix('.zip')
        output_path = str(dst_dir)
        # todo use zipfile and write mechanism. Can then write file by file and use this to update export job
        datetime_start = datetime.datetime.now()
        n_files_written = 0
        with zipfile.ZipFile(output_path,'w', zipfile.ZIP_DEFLATED) as zip_file:
            for entry in Path(src_dir).rglob("*"):
                zip_file.write(entry, entry.relative_to(src_dir))
                n_files_written += 1
                datetime_now = datetime.datetime.now()
                if (datetime_now - datetime_start).total_seconds() > self._update_job_period_s:
                    db_controller.update_export_job(job_id, n_files_written)
        db_controller.update_export_job(job_id, n_files_written)
        return n_files_written


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
