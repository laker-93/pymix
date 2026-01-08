import datetime
import logging

import mimetypes
import shutil
import uuid
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Tuple, Optional, Dict
from zipfile import ZipFile

import taglib
from filetype import filetype
from watchfiles import awatch, Change

from tinydb import TinyDB

from pymix.controllers.db_controller import DbController
from pymix.model.original_track_meta import OriginalTracks
from pymix.model.subboxtrack import SubBoxTrack
from pymix.utils.tag_subbox_id import tag_subbox_id

logger = logging.getLogger(__name__)



async def trigger_processing(recv_stream, rekordbox_xml_controller):
    async with recv_stream:
        async for user in recv_stream:
            logger.info(f'processing for user {user}...')
            await rekordbox_xml_controller.consume_from_filebrowser(user, public=False, watch=True)



async def poll_watchdir(watchpaths: list[Path], send_stream, db_controller):
    poll_time = 10
    user_paths = defaultdict(list)
    user_n_timeouts_no_update = defaultdict(int)
    maxed_out_users = set()

    async with send_stream:
        async for changes in awatch(*watchpaths, yield_on_timeout=True, rust_timeout=1000):
            for user in user_n_timeouts_no_update.keys():
                user_n_timeouts_no_update[user] += 1
                if user_n_timeouts_no_update[user] == poll_time and user not in maxed_out_users:
                    logger.info(f"user {user} has files to process and has had no update in the past {poll_time} seconds")
                    await send_stream.send(user)
                    user_paths[user].clear()
            for change in changes:
                if change[0] == Change.added:
                    added_path = Path(change[1])
                    user = added_path.parts[added_path.parts.index('user-updownloads') + 1]
                    user_paths[user].append(added_path)
                    user_n_timeouts_no_update[user] = 0
                    size_to_import = 0
                    for p in user_paths[user]:
                        if p.exists():
                            size_to_import += p.stat().st_size
                        else:
                            logger.info(f'path {p} does not exist')
                    if db_controller.user_library_size_exceeded(user, size_to_import):
                        logger.error(f'exceeded library size for user {user}')
                        maxed_out_users.add(user)
                        # todo some how notify front end and block user from uploading more



class FileBrowserFileHandler:
    def __init__(
            self,
            zip_name: str,
            serving_music_path_base: str,
            filebrowser_data_path_uploads: str,
            filebrowser_data_path_watch: str,
            filebrowser_data_path_downloads: str,
            beets_data_path: str,
            beets_data_path_public: str,
            update_job_period_s: int,
    ):
        self._zip_name = zip_name
        self._serving_music_path_base = serving_music_path_base.removesuffix('/')
        self._filebrowser_data_path_uploads = filebrowser_data_path_uploads
        self._filebrowser_data_path_watch = filebrowser_data_path_watch
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
            mime_type = filetype.guess_mime(f)
            if mime_type is None:
                mime_type = mimetypes.guess_type(str(f))[0]
            if mime_type is None:
                logger.error(f'skipping file {f}')
                continue
            if f.name.lower() == 'subcrates.zip':
                subcrate_path = f
            elif mime_type.split('/')[0] == 'audio':
                audio_path = src_path
            if audio_path and subcrate_path:
                break
        assert subcrate_path
        assert audio_path
        return subcrate_path, audio_path

    def tag_staging_with_subbox_id(self, user: str, tracks: OriginalTracks):
        src_path = Path(
            self._filebrowser_data_path_uploads.format(user=user)
        )

        for f in src_path.rglob('*'):
            if f.is_file():
                if mime_type is None:
                    mime_type = mimetypes.guess_type(str(f))[0]
                if mime_type is None:
                    logger.error(f'skipping file {f}')
                    continue
                if mime_type.split('/')[0] == 'audio':
                    for track in tracks.tracks:
                        if track.stagingLocation in str(f):
                            subbox_id = tag_subbox_id(f)
                            if subbox_id:
                                track.subbox_id = subbox_id






    def get_xml_data_path(self, user: str) -> tuple[Path, Optional[Path], Optional[Path]]:
        src_path = Path(
            self._filebrowser_data_path_uploads.format(user=user)
        )
        xml_path = None
        zip_path = None
        audio_path = None
        for f in src_path.rglob('*'):
            if f.is_file():
                mime_type = filetype.guess_mime(f)
                if mime_type is None:
                    mime_type = mimetypes.guess_type(str(f))[0]
                if mime_type is None:
                    logger.error(f'skipping file {f}')
                    continue
                if mime_type == 'application/xml':
                    xml_path = f
                elif f.name.endswith('.zip') and 'macosx' not in f.name.lower():
                    zip_path = f
                elif mime_type.split('/')[0] == 'audio':
                    audio_path = src_path
            if zip_path and xml_path:
                break

        assert xml_path
        return xml_path, zip_path, audio_path

    def get_size_of_import(self, user: str) -> Dict[str, int]:
        src_path = Path(self._filebrowser_data_path_uploads.format(user=user))
        audio_files_zip = None
        n_files = 0
        total_size = 0

        for f in src_path.iterdir():
            if f.is_file():
                if f.name.endswith('.zip'):
                    audio_files_zip = f
                    break
                # todo use filetype
                n_files += 1
                total_size += f.stat().st_size

        if audio_files_zip:
            with ZipFile(audio_files_zip) as zip:
                files = zip.namelist()
                for f in files:
                    if 'macosx' in str(f).lower():
                        # ignore meta info
                        continue
                    mimestart = mimetypes.guess_type(str(f))[0]
                    if mimestart:
                        mimecategory = mimestart.split('/')[0]
                        if mimecategory == 'audio':
                            logger.info(f'found audio file {f}')
                            n_files += 1
                            total_size += zip.getinfo(f).file_size

        return {'n_tracks': n_files, 'size_tracks': total_size}


    def sync(self, username: str, tracks_to_zip: list[SubBoxTrack]) -> Tuple[int, Path]:
        dst_dir = Path(self._filebrowser_data_path_downloads.format(user=username)) / self._zip_name
        output_path = str(dst_dir.with_suffix('.zip'))
        n_files_written = 0
        src_dir = self._serving_music_path_base.format(user=username)
        if len(tracks_to_zip) > 0:
            with zipfile.ZipFile(output_path,'w', zipfile.ZIP_DEFLATED) as zip_file:
                for entry in tracks_to_zip:
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


    def stage_for_import(self, username: str, public: bool, watch: bool):
        """
        copy files from filebrowser to beets input data path.
        """
        if watch:
            src_dir = self._filebrowser_data_path_watch.format(user=username)
        else:
            src_dir = self._filebrowser_data_path_uploads.format(user=username)
        if public:
            dst_dir = self._beets_data_path_public
        else:
            dst_dir = self._beets_data_path.format(user=username)
        logger.info(f'staging for import. Extracting from {src_dir} to {dst_dir}')
        n_files = 0
        for entry in Path(src_dir).iterdir():
            n_files += 1
            if entry.is_file():
                if entry.suffix == '.zip':
                    with zipfile.ZipFile(entry, 'r') as zip_ref:
                        zip_ref.extractall(dst_dir)
                else:
                    file_name = entry.parts[-1]
                    # must use shutil as pathlib doesn't work cross filesystem as fb-data path is on a docker volume
                    shutil.copy(entry, Path(dst_dir) / file_name)
                    #entry.rename(Path(dest_dir) / file_name)
            elif entry.is_dir():
                shutil.copytree(entry, Path(dst_dir) / entry.name, dirs_exist_ok=True)
        logger.info(f'extracted {n_files} files to {dst_dir}')

    def remove_fb_data_path(self, username, watch: bool = False):
        if watch:
            src_dir = Path(self._filebrowser_data_path_watch.format(user=username))
        else:
            src_dir = Path(self._filebrowser_data_path_uploads.format(user=username))
        logger.info(f'removing contents of {src_dir}')
        for filepath in src_dir.iterdir():
            if filepath.is_dir():
                shutil.rmtree(filepath)
            else:
                filepath.unlink()
