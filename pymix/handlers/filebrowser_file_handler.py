import datetime
import logging

import mimetypes
import shutil
import zipfile
from pathlib import Path
from typing import Tuple
from zipfile import ZipFile

from tinydb import TinyDB

from pymix.controllers.db_controller import DbController
from pymix.model.subboxtrack import SubBoxTrack

logger = logging.getLogger(__name__)


class FileBrowserFileHandler:
    def __init__(
            self,
            zip_name: str,
            serving_music_path_base: str,
            filebrowser_data_path_uploads: str,
            filebrowser_data_path_downloads: str,
            beets_data_path: str,
            beets_data_path_public: str,
            update_job_period_s: int,
    ):
        self._zip_name = zip_name
        self._serving_music_path_base = serving_music_path_base.removesuffix('/')
        self._filebrowser_data_path_uploads = filebrowser_data_path_uploads
        self._filebrowser_data_path_downloads = filebrowser_data_path_downloads
        self._beets_data_path = beets_data_path
        self._beets_data_path_public = beets_data_path_public
        self._update_job_period_s = update_job_period_s
        self._mimetypes = mimetypes.init()

    def get_xml_output_path(self, username: str) -> Path:
        src_path = Path(
            self._filebrowser_data_path_downloads.format(user=username)
        )
        src_path.mkdir(exist_ok=True)
        xml_path = src_path / 'subbox_rb_export.xml'
        # ensure starting from a clean state.
        # multiple exports could pick up the xml from a previous run.
        xml_path.unlink(missing_ok=True)
        return xml_path

    def get_crate_output_path(self, username: str) -> Path:
        src_path = Path(
            self._filebrowser_data_path_downloads.format(user=username)
        )
        src_path.mkdir(exist_ok=True)
        path = src_path
        return path


    def get_subcrate_audio_path(self, user: str) -> tuple[Path, Path]:
        src_path = Path(
            self._filebrowser_data_path_uploads.format(user=user)
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
            self._filebrowser_data_path_uploads.format(user=user)
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
            self._filebrowser_data_path_uploads.format(user=user)
        )
        audio_files_zip = None
        n_files = 0
        for f in src_path.iterdir():
            if f.is_file():
                if f.name.endswith('.zip'):
                    audio_files_zip = f
                    break
                mimestart = mimetypes.guess_type(str(f))[0]
                if mimestart:
                    mimecategory = mimestart.split('/')[0]
                    if mimecategory == 'audio':
                        logger.info(f'found audio file {f}')
                        n_files += 1

        if audio_files_zip:
            with ZipFile(audio_files_zip) as zip:
                files = zip.namelist()
                for f in files:
                    if '__MACOSX' in str(f):
                        # ignore meta info
                        continue
                    mimestart = mimetypes.guess_type(str(f))[0]
                    if mimestart:
                        mimecategory = mimestart.split('/')[0]
                        if mimecategory == 'audio':
                            logger.info(f'found audio file {f}')
                            n_files += 1
        return n_files


    def sync(self, username: str, client_tracks: list[SubBoxTrack], server_tracks: dict[int, SubBoxTrack]) -> Tuple[int, Path]:
        client_tracks_to_remove = []
        for client_track in client_tracks:
            if client_track.sub_track_id not in server_tracks:
                client_tracks_to_remove.append(client_track)
            else:
                server_tracks.pop(client_track.sub_track_id)
        tracks_to_zip = server_tracks

        dst_dir = Path(self._filebrowser_data_path_downloads.format(user=username)) / self._zip_name
        output_path = str(dst_dir.with_suffix('.zip'))
        n_files_written = 0
        src_dir = self._serving_music_path_base.format(user=username)
        with zipfile.ZipFile(output_path,'w', zipfile.ZIP_DEFLATED) as zip_file:
            for entry in tracks_to_zip.values():
                entry_dir = str(entry.path).removeprefix('/' + self._zip_name)
                p = Path(src_dir + entry_dir)
                zip_file.write(p, Path(self._zip_name) / p.relative_to(src_dir))
                n_files_written += 1
        return n_files_written, dst_dir

    def export_subsonic_music(self, db_path: str, app_env: str, username: str, job_id: str) -> int:
        db_controller = DbController(TinyDB(db_path), app_env)
        src_dir = self._serving_music_path_base.format(user=username)
        dst_dir = Path(self._filebrowser_data_path_downloads.format(user=username)) / self._zip_name
        output_path = str(dst_dir.with_suffix('.zip'))
        datetime_start = datetime.datetime.now()
        n_files_written = 0
        with zipfile.ZipFile(output_path,'w', zipfile.ZIP_DEFLATED) as zip_file:
            for entry in Path(src_dir).rglob("*"):
                zip_file.write(entry, Path(self._zip_name) / entry.relative_to(src_dir))
                n_files_written += 1
                datetime_now = datetime.datetime.now()
                if (datetime_now - datetime_start).total_seconds() > self._update_job_period_s:
                    db_controller.update_export_job(job_id, n_files_written)
        db_controller.update_export_job(job_id, n_files_written)
        return n_files_written


    def stage_for_import(self, username: str, public: bool):
        """
        copy files from filebrowser to beets input data path.
        """
        src_dir = self._filebrowser_data_path_uploads.format(user=username)
        if public:
            dst_dir = self._beets_data_path_public
        else:
            dst_dir = self._beets_data_path.format(user=username)
        logger.info(f'staging for import. Extracting from {src_dir} to {dst_dir}')
        for entry in Path(src_dir).iterdir():
            if entry.is_file():
                if entry.suffix == '.zip':
                    with zipfile.ZipFile(entry, 'r') as zip_ref:
                        zip_ref.extractall(dst_dir)
                else:
                    file_name = entry.parts[-1]
                    # must use shutil as pathlib doesn't work cross filesystem as fb-data path is on a docker volume
                    shutil.copy(entry, Path(dst_dir) / file_name)
                    #entry.rename(Path(dest_dir) / file_name)

    def remove_fb_data_path(self, username):
        logger.info(f'removing contents of {self._filebrowser_data_path_uploads.format(user=username)}')
        for filepath in Path(self._filebrowser_data_path_uploads.format(user=username)).iterdir():
            if filepath.is_dir():
                shutil.rmtree(filepath)
            else:
                filepath.unlink()
