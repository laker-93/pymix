import datetime
import logging
import time
import mimetypes

import shutil
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Tuple, Optional, Dict, Any
from zipfile import ZipFile

import music_tag
from watchfiles import awatch, Change

from pymix.controllers.db_controller import DbController
from pymix.model.original_track_meta import OriginalTracks, OriginalTrackMeta
from pymix.model.subboxtrack import SubBoxTrack
from pymix.utils.tag_subbox_id import tag_subbox_id
from pymix.utils.utility import detect_audio_type, detect_audio_type_with_reason

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {
    '.mp3', '.flac', '.m4a', '.aac', '.ogg', '.opus', '.wav', '.aiff', '.aif', '.wma', '.alac', '.zip',
}


def _has_audio_files(directory: Path) -> bool:
    """Check whether a directory contains at least one audio file (or zip)."""
    for entry in directory.rglob('*'):
        if entry.is_file() and entry.suffix.lower() in AUDIO_EXTENSIONS:
            return True
    return False


def _all_files_stable(directory: Path, stable_seconds: float) -> bool:
    """Return True only if every audio file's mtime is at least *stable_seconds* ago.

    This guards against partially-downloaded files whose filesystem events
    may not have been delivered to watchfiles (e.g. writes via an open fd,
    or temp-file-then-rename patterns).
    """
    now = time.time()
    for entry in directory.rglob('*'):
        if entry.is_file() and entry.suffix.lower() in AUDIO_EXTENSIONS:
            try:
                age = now - entry.stat().st_mtime
                if age < stable_seconds:
                    logger.info(f'watch: file {entry.name} still unstable (modified {age:.1f}s ago)')
                    return False
            except OSError:
                # File may have been removed between rglob and stat
                continue
    return True



async def trigger_processing(recv_stream, rekordbox_xml_controller, db_controller: DbController):
    async with recv_stream:
        async for user in recv_stream:
            logger.info(f'watch import: processing for user {user}...')
            job_id = None
            success = True
            try:
                job_id = db_controller.create_import_job(user, number_of_tracks_to_import=0, total_n_imported_tracks=0)
                await rekordbox_xml_controller.consume_from_filebrowser(user, public=False, watch=True)
            except Exception:
                success = False
                logger.exception(f'watch import: failed for user {user}')
            finally:
                if job_id:
                    db_controller.job_completed(job_id, success)
                logger.info(f'watch import: finished for user {user} (success={success})')


async def poll_watchdir(user_root: Path, watch_subdir: str, send_stream, db_controller):
    """Watch user_root for file additions in any user's watch directory.

    Watches the entire user_root tree so newly created users are picked up
    automatically without needing a restart. Only reacts to files under
    ``<user_root>/<username>/<watch_subdir>/``.

    Uses a time-based debounce: after the last file addition or modification
    for a user, waits ``DEBOUNCE_SECONDS`` of inactivity before triggering
    import. This ensures in-progress downloads (which produce modified events)
    are complete before processing.
    """
    DEBOUNCE_SECONDS = 15
    user_last_change: dict[str, float] = {}
    user_pending_size: dict[str, int] = defaultdict(int)
    maxed_out_users: set[str] = set()

    async with send_stream:
        async for changes in awatch(user_root, yield_on_timeout=True, rust_timeout=1000):
            now = time.monotonic()

            for change_type, change_path in changes:
                if change_type not in (Change.added, Change.modified):
                    continue
                path = Path(change_path)
                try:
                    rel = path.relative_to(user_root)
                except ValueError:
                    continue
                # Expect structure: <username>/<watch_subdir>/...
                parts = rel.parts
                if len(parts) < 2 or parts[1] != watch_subdir:
                    continue
                user = parts[0]

                if user in maxed_out_users:
                    continue

                if path.is_file():
                    user_pending_size[user] += path.stat().st_size
                user_last_change[user] = now

                exceeded, _, _ = db_controller.user_library_size_exceeded(user, user_pending_size[user])
                if exceeded:
                    logger.error(f'watch: library size exceeded for user {user}')
                    maxed_out_users.add(user)
                    user_pending_size.pop(user, None)
                    user_last_change.pop(user, None)

            # Check which users have passed the debounce window
            ready_users = [
                u for u, last in user_last_change.items()
                if now - last >= DEBOUNCE_SECONDS and u not in maxed_out_users
            ]
            for user in ready_users:
                watch_dir = user_root / user / watch_subdir
                if not _has_audio_files(watch_dir):
                    logger.info(f'watch: no audio files in watch dir for user {user}, skipping')
                    user_last_change.pop(user)
                    user_pending_size.pop(user, 0)
                    continue
                if not _all_files_stable(watch_dir, DEBOUNCE_SECONDS):
                    logger.info(f'watch: files still being written for user {user}, deferring import')
                    # Reset debounce so we re-check after another DEBOUNCE_SECONDS
                    user_last_change[user] = now
                    continue
                n_bytes = user_pending_size.get(user, 0)
                logger.info(
                    f'watch: triggering import for user {user} '
                    f'(~{n_bytes / 1024 / 1024:.1f} MB, debounce complete)'
                )
                await send_stream.send(user)
                user_last_change.pop(user)
                user_pending_size.pop(user, 0)



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
            db_controller: DbController
    ):
        self._zip_name = zip_name
        self._serving_music_path_base = serving_music_path_base.removesuffix('/')
        self._filebrowser_data_path_uploads = filebrowser_data_path_uploads
        self._filebrowser_data_path_watch = filebrowser_data_path_watch
        self._filebrowser_data_path_downloads = filebrowser_data_path_downloads
        self._beets_data_path = beets_data_path
        self._beets_data_path_public = beets_data_path_public
        self._update_job_period_s = update_job_period_s
        self._db_controller = db_controller

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

    def get_subcrate_audio_path(self, user: str) -> tuple[Path, Optional[Path], Optional[Path]]:
        src_path = Path(
            self._filebrowser_data_path_uploads.format(user=user)
        )
        subcrate_path = None
        audio_path = None
        zip_path = None
        for f in src_path.rglob('*'):
            if not f.is_file():
                continue
            if f.name.lower() == 'all-crates.zip':
                subcrate_path = f
            elif f.name.endswith('.zip') and 'macosx' not in f.name.lower() and 'all-crates' not in f.name.lower():
                zip_path = f
            elif detect_audio_type(f) is not None:
                audio_path = src_path
            if audio_path and subcrate_path:
                break
        assert subcrate_path
        return subcrate_path, zip_path, audio_path

    def tag_staging_with_subbox_id(self, user: str, tracks: OriginalTracks) -> Dict[str, Any]:
        src_path = Path(
            self._filebrowser_data_path_uploads.format(user=user)
        )

        report: Dict[str, Any] = {
            'tagged_count': 0,
            'already_tagged_count': 0,
            'untagged_count': 0,
            'untagged': [],
        }

        # Build a lookup by staging location so we can explain why each track was skipped.
        tracks_by_staging = {track.stagingLocation: track for track in tracks.tracks}
        matched_staging_locations: set[str] = set()

        for f in src_path.rglob('*'):
            if not f.is_file():
                continue

            file_path = str(f)
            track = None
            for staging_location, candidate in tracks_by_staging.items():
                if staging_location in file_path:
                    track = candidate
                    matched_staging_locations.add(staging_location)
                    break
            if track is None:
                continue

            audio_type, non_audio_reason = detect_audio_type_with_reason(f)
            if audio_type is None:
                logger.error(
                    'tag_staging_with_subbox_id: non-audio file for stagingLocation=%s file=%s reason=%s',
                    track.stagingLocation,
                    file_path,
                    non_audio_reason,
                )
                report['untagged'].append({
                    'stagingLocation': track.stagingLocation,
                    'file': file_path,
                    'reason': non_audio_reason,
                })
                continue

            existing_subbox_id = track.subbox_id
            subbox_id = tag_subbox_id(f)
            if subbox_id:
                track.subbox_id = subbox_id
                if existing_subbox_id and existing_subbox_id == subbox_id:
                    report['already_tagged_count'] += 1
                else:
                    report['tagged_count'] += 1
            else:
                report['untagged'].append({
                    'stagingLocation': track.stagingLocation,
                    'file': file_path,
                    'reason': 'tag_subbox_id_returned_none',
                })

        for track in tracks.tracks:
            if track.subbox_id is not None:
                continue
            if track.stagingLocation not in matched_staging_locations:
                report['untagged'].append({
                    'stagingLocation': track.stagingLocation,
                    'file': None,
                    'reason': 'no_matching_file_for_staging_location',
                })

        report['untagged_count'] = len(report['untagged'])
        logger.info(
            'tag_staging_with_subbox_id summary for user %s: tagged=%s already_tagged=%s untagged=%s',
            user,
            report['tagged_count'],
            report['already_tagged_count'],
            report['untagged_count'],
        )
        if report['untagged_count']:
            logger.error('tag_staging_with_subbox_id untagged details: %s', report['untagged'])

        return report






    def get_xml_data_path(self, user: str) -> tuple[Path, Optional[Path], Optional[Path]]:
        src_path = Path(
            self._filebrowser_data_path_uploads.format(user=user)
        )
        xml_path = None
        zip_path = None
        audio_path = None
        counters = {
            "n_file": 0,
            "n_xml": 0,
            "n_audio": 0,
            "n_skipped_files": 0
        }
        for f in src_path.rglob('*'):
            if f.is_file():
                counters["n_file"] += 1
                guessed_mime = mimetypes.guess_type(str(f))[0]
                if guessed_mime in ('application/xml', 'text/xml'):
                    xml_path = f
                    counters["n_xml"] += 1
                elif f.name.endswith('.zip') and 'macosx' not in f.name.lower():
                    zip_path = f
                elif detect_audio_type(f) is not None:
                    audio_path = src_path
                    counters["n_audio"] += 1
            if zip_path and xml_path:
                break

        logger.info(f'parsed {counters} from {src_path}')
        assert xml_path
        return xml_path, zip_path, audio_path

    def get_size_of_import(self, user: str) -> Dict[str, int]:
        src_path = Path(self._filebrowser_data_path_uploads.format(user=user))
        audio_files_zip = None
        n_files = 0
        total_size = 0

        for f in src_path.iterdir():
            if f.is_file():
                if f.name.endswith('.zip') and f.name != 'all-crates.zip':
                    audio_files_zip = f
                    logger.info(f'adding audio zip {f}')
                    break
        for f in src_path.rglob('*'):
            if f.is_file():
                if detect_audio_type(f) is not None:
                    n_files += 1
                    total_size += f.stat().st_size

        if audio_files_zip:
            with ZipFile(audio_files_zip) as zip:
                files = zip.namelist()
                for f in files:
                    if 'macosx' in str(f).lower():
                        # ignore meta info
                        continue
                    if f.endswith('.crate'):
                        # ignore crate
                        continue
                    # Zip entries are not real paths on disk; fall back to extension check
                    suffix = Path(f).suffix.lower()
                    if suffix in AUDIO_EXTENSIONS - {'.zip'}:
                        logger.info(f'found audio file {f}')
                        n_files += 1
                        total_size += zip.getinfo(f).file_size

        return {'n_tracks': n_files, 'size_tracks': total_size}


    def sync(self, username: str, tracks_to_zip: list[SubBoxTrack]) -> Tuple[int, Path]:
        dst_dir = Path(self._filebrowser_data_path_downloads.format(user=username)) / self._zip_name
        output_path = str(dst_dir.with_suffix('.zip'))
        n_files_written = 0
        src_dir = self._serving_music_path_base
        if len(tracks_to_zip) > 0:
            with zipfile.ZipFile(output_path,'w', zipfile.ZIP_DEFLATED) as zip_file:
                for entry in tracks_to_zip:
                    entry_dir = str(entry.path).removeprefix('/' + self._zip_name)
                    p = Path(src_dir + entry_dir)
                    zip_file.write(p, Path(self._zip_name) / p.relative_to(src_dir))
                    n_files_written += 1
        return n_files_written, dst_dir

    def export_subsonic_music(self, db_config: dict, app_env: str, username: str, job_id: str) -> int:
        from pymix.factories.create_db_session import create_db_session
        session_factory = create_db_session(
            db_host=db_config["host"],
            db_port=db_config["port"],
        )
        db_controller = DbController(session_factory, app_env, 0)
        src_dir = f'{self._serving_music_path_base}/{username}'
        dst_dir = Path(self._filebrowser_data_path_downloads.format(user=username)) / self._zip_name
        output_path = str(dst_dir.with_suffix('.zip'))
        datetime_start = datetime.datetime.now()
        n_files_written = 0
        with zipfile.ZipFile(output_path,'w', zipfile.ZIP_DEFLATED) as zip_file:
            for entry in Path(src_dir).rglob("*"):
                if not entry.is_file():
                    continue
                # Store paths relative to /private-music/<username>/ so the zip extracts to
                # <zip location>/<artist>/<album>/<track> without private server prefixes.
                entry_to_write = entry.relative_to(src_dir)
                logger.info(f'exporting {entry} as {entry_to_write} for user {username}')
                zip_file.write(entry, entry_to_write)
                n_files_written += 1
                datetime_now = datetime.datetime.now()
                if (datetime_now - datetime_start).total_seconds() > self._update_job_period_s:
                    db_controller.update_export_job(job_id, n_files_written)
        db_controller.update_export_job(job_id, n_files_written)
        return n_files_written


    def stage_for_import(self, username: str, public: bool, watch: bool):
        """
        Stage files from filebrowser to beets input data path.
        When watch=True, files are moved (not copied) so new arrivals during
        a slow import are left untouched for the next cycle.
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
        for entry in Path(src_dir).iterdir():
            if entry.is_file():
                if entry.suffix == '.zip':
                    with zipfile.ZipFile(entry, 'r') as zip_ref:
                        zip_ref.extractall(dst_dir)
                    if watch:
                        entry.unlink()
                else:
                    file_name = entry.parts[-1]
                    if watch:
                        shutil.move(str(entry), Path(dst_dir) / file_name)
                    else:
                        # must use shutil as pathlib doesn't work cross filesystem as fb-data path is on a docker volume
                        shutil.copy(entry, Path(dst_dir) / file_name)
            elif entry.is_dir():
                shutil.copytree(entry, Path(dst_dir) / entry.name, dirs_exist_ok=True)
                if watch:
                    shutil.rmtree(entry)

        tracks = OriginalTracks(tracks=[])

        for file_path in Path(dst_dir).rglob("*"):
            if not file_path.is_file():
                continue

            subbox_id = tag_subbox_id(file_path)
            try:
                f = music_tag.load_file(str(file_path))
            except Exception:
                logger.error(f'unable to parse {file_path}')
            else:
                track = OriginalTrackMeta(
                    userLocation=None,
                    stagingLocation=str(file_path),
                    originalName=str(f.get('tracktitle', '')),
                    originalArtist=str(f.get('artist', '')),
                    originalAlbum=str(f.get('album', '')),
                    subbox_id=subbox_id,
                )
                tracks.tracks.append(track)

        logger.info(f"constructed metadata for {len(tracks.tracks)} audio files")
        self._db_controller.save_original_track_meta(username, tracks)

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
