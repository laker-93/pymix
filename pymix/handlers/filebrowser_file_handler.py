import datetime
import logging
import time
import mimetypes

import shutil
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Tuple, Optional, Dict, Any, List
from zipfile import ZipFile

import music_tag
from watchfiles import awatch, Change

from pymix.controllers.db_controller import DbController
from pymix.model.original_track_meta import OriginalTracks, OriginalTrackMeta, UploadAttempt
from pymix.model.subboxtrack import SubBoxTrack
from pymix.services.import_progress import failure_reason
from pymix.utils.tag_subbox_id import get_subbox_id, tag_subbox_id
from pymix.utils.utility import AUDIO_EXTENSIONS, detect_audio_type, detect_audio_type_with_reason

logger = logging.getLogger(__name__)

# The Serato crate bundle the client uploads. It lives in the same uploads
# directory as the user's audio, so every scan of that directory that is looking
# for an audio zip has to step over it.
CRATE_ZIP_NAME = 'all-crates.zip'


def is_crate_zip(f: Path) -> bool:
    return f.name.lower() == CRATE_ZIP_NAME


def is_audio_zip(f: Path) -> bool:
    """
    Is this the zip of the user's music, as opposed to the crate bundle or macOS'
    resource-fork sidecar?

    Worth having in one place: the three scans of the uploads directory each
    answered it slightly differently, and the Rekordbox one did not exclude the
    crate bundle at all -- so a Serato import that failed before pymix could clear
    the directory left an all-crates.zip behind for the next Rekordbox import to
    stage into beets as if it were music.
    """
    name = f.name.lower()
    return name.endswith('.zip') and 'macosx' not in name and not is_crate_zip(f)


@dataclass
class AttemptFiles:
    """
    What one import may stage from ``uploads/{user}``: the files the attempt's
    /sync/map_meta tagged, and nothing else (#38).

    ``refused`` are the attempt's own files that cannot go in: the file is gone,
    or no longer carries the SUBBOX_ID map_meta gave it. Staging those would put
    a track in the library that nothing can match or delete by id, so they are
    reported rather than imported. ``leftovers`` are audio files no attempt asked
    for, usually what an earlier upload left behind. They are only logged. They
    were never part of what the user asked for this time.
    """
    root: Path
    files: List[Path] = field(default_factory=list)
    refused: List[Tuple[str, str]] = field(default_factory=list)
    leftovers: List[Path] = field(default_factory=list)

    def refused_warning(self) -> str:
        if not self.refused:
            return ''
        path, reason = self.refused[0]
        return (
            f'{len(self.refused)} uploaded track(s) were not imported because they could not be '
            f'identified ({path}: {reason}). Upload them again.'
        )


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
            reason = ""
            try:
                job_id = db_controller.create_import_job(user, number_of_tracks_to_import=0, total_n_imported_tracks=0)
                await rekordbox_xml_controller.consume_from_filebrowser(user, public=False, watch=True)
            except Exception as ex:
                success = False
                reason = failure_reason(ex)
                logger.exception(f'watch import: failed for user {user}')
            finally:
                if job_id:
                    db_controller.job_completed(job_id, success, reason)
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
    user_pending_files: dict[str, dict[str, int]] = defaultdict(dict)
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
                    user_pending_files[user][str(path)] = path.stat().st_size
                user_last_change[user] = now

                pending_size = sum(user_pending_files[user].values())
                exceeded, _, _ = db_controller.user_library_size_exceeded(user, pending_size)
                if exceeded:
                    logger.error(f'watch: library size exceeded for user {user}')
                    maxed_out_users.add(user)
                    user_pending_files.pop(user, None)
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
                    user_pending_files.pop(user, None)
                    continue
                if not _all_files_stable(watch_dir, DEBOUNCE_SECONDS):
                    logger.info(f'watch: files still being written for user {user}, deferring import')
                    # Reset debounce so we re-check after another DEBOUNCE_SECONDS
                    user_last_change[user] = now
                    continue
                n_bytes = sum(user_pending_files.get(user, {}).values())
                logger.info(
                    f'watch: triggering import for user {user} '
                    f'(~{n_bytes / 1024 / 1024:.1f} MB, debounce complete)'
                )
                await send_stream.send(user)
                user_last_change.pop(user)
                user_pending_files.pop(user, None)



class FileBrowserFileHandler:
    def __init__(
            self,
            local_user_music_stem: str,
            zip_name: str,
            serving_music_path_base: str,
            filebrowser_data_path_uploads: str,
            filebrowser_data_path_watch: str,
            filebrowser_data_path_downloads: str,
            beets_data_path: str,
            beets_data_path_public: str,
            update_job_period_s: int,
            db_controller: DbController,
    ):
        self._local_user_music_stem = local_user_music_stem
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

    def get_name_in_export_zip(self, path: Path) -> str:
        """Where a non-track file (the Rekordbox XML) goes inside the export zip.

        Under the same music/ prefix every track gets, so the zip has exactly ONE
        top-level entry. macOS' Archive Utility wraps an archive in an extra folder
        named after it only when there's more than one top-level entry, and that
        wrapper silently invalidated every Location in the XML: the tracks landed at
        <extract>/music/music/<artist>/... while the XML pointed at <extract>/...
        """
        stem = self._local_user_music_stem
        return str(Path(stem) / path.name) if stem else path.name

    def get_downloads_dir(self, username: str) -> Path:
        """The directory sync/rekordbox exports are written to for this user.

        Read-only lookup (unlike get_xml_output_path, this
        doesn't create the directory) — used by /sync/download to resolve a
        filename to stream back, where a missing directory should just mean 404.
        """
        return Path(self._filebrowser_data_path_downloads.format(user=username))

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
            if is_crate_zip(f):
                subcrate_path = f
            elif is_audio_zip(f):
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
            # Each file tagged, by its path under uploads/, with the id it was
            # tagged with: what the import is allowed to stage (#38).
            'staged': {},
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
                report['staged'][str(f.relative_to(src_path))] = subbox_id
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
                elif is_audio_zip(f):
                    zip_path = f
                elif detect_audio_type(f) is not None:
                    audio_path = src_path
                    counters["n_audio"] += 1
            if zip_path and xml_path:
                break

        logger.info(f'parsed {counters} from {src_path}')
        assert xml_path
        return xml_path, zip_path, audio_path

    def uploads_dir(self, user: str) -> Path:
        return Path(self._filebrowser_data_path_uploads.format(user=user))

    def select_attempt_files(self, user: str, attempt: UploadAttempt) -> AttemptFiles:
        """
        The audio under ``uploads/{user}`` that this attempt may import (#38).

        A file is staged only if the attempt's /sync/map_meta tagged it, and it
        still carries that id when read back. The read-back matters:
        ``tag_subbox_id`` can return an id it failed to write, and an untagged
        file in beets can't be matched, deleted by id or exported. Everything
        else in the directory is a leftover and is not staged.
        """
        root = self.uploads_dir(user)
        selected = AttemptFiles(root=root)
        found: set[str] = set()
        for f in sorted(root.rglob('*')) if root.is_dir() else []:
            if not f.is_file():
                continue
            relative_path = str(f.relative_to(root))
            expected = attempt.files.get(relative_path)
            if expected is None:
                # By extension, not by sniffing it: a leftover is only logged, and
                # an upload can leave thousands of them.
                if f.suffix.lower() in AUDIO_EXTENSIONS - {'.zip'}:
                    selected.leftovers.append(f)
                continue
            found.add(relative_path)
            try:
                subbox_id = get_subbox_id(f)
            except Exception as ex:
                selected.refused.append((relative_path, f'unreadable tags: {ex!r}'))
                continue
            if subbox_id != expected:
                reason = 'no SUBBOX_ID tag' if subbox_id is None else f'SUBBOX_ID is {subbox_id}, expected {expected}'
                selected.refused.append((relative_path, reason))
                continue
            selected.files.append(f)
        for relative_path in sorted(attempt.files.keys() - found):
            selected.refused.append((relative_path, 'file is no longer in uploads'))

        logger.info(
            f'upload attempt for {user}: {len(attempt.files)} file(s) requested, '
            f'{len(selected.files)} to stage, {len(selected.refused)} refused, '
            f'{len(selected.leftovers)} leftover(s) not staged'
        )
        if selected.refused:
            logger.error(f'upload attempt for {user}: refused {selected.refused}')
        if selected.leftovers:
            sample = [str(f.relative_to(root)) for f in selected.leftovers[:5]]
            logger.warning(
                f'upload attempt for {user}: not staging {len(selected.leftovers)} file(s) '
                f'no attempt asked for, e.g. {sample}'
            )
        return selected

    def get_size_of_import(self, user: str, attempt: Optional[UploadAttempt] = None) -> Dict[str, int]:
        """
        How many tracks, and how many bytes, an import would add.

        With ``attempt``, only that attempt's files count (what the Rekordbox and
        Serato imports stage). They are not re-read here: this runs in the request,
        and map_meta has already checked each one is audio. Without it, everything
        in ``uploads/`` counts, including any audio zip, for /beets/import, which
        still consumes the whole directory.
        """
        if attempt is not None:
            root = self.uploads_dir(user)
            files = [f for f in (root / p for p in attempt.files) if f.is_file()]
            return {'n_tracks': len(files), 'size_tracks': sum(f.stat().st_size for f in files)}
        src_path = self.uploads_dir(user)
        audio_files_zip = None
        n_files = 0
        total_size = 0

        for f in src_path.iterdir():
            if f.is_file():
                if is_audio_zip(f):
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


    def sync(
        self,
        username: str,
        tracks_to_zip: list[SubBoxTrack],
        extra_files: Optional[list[Tuple[Path, str]]] = None,
    ) -> Tuple[int, Path]:
        """Zip the given tracks for download, plus any (source, name-in-zip) extras.

        `extra_files` exists for files that don't live under the user's music root
        and so have no path relative to it — the Rekordbox XML, which is written to
        the downloads dir. Callers should name it via get_name_in_export_zip so it
        lands under the same music/ prefix as the tracks.
        """
        src_dir = self.get_user_music_root(username)
        files_to_zip: list[Path] = []
        for track in tracks_to_zip:
            file_path: Optional[Path] = None
            if track.pymix_path:
                file_path = Path(track.pymix_path)
            elif track.path:
                entry_dir = str(track.path).removeprefix('/' + self._local_user_music_stem).lstrip('/')
                file_path = src_dir / entry_dir

            if file_path is None:
                logger.error(f'sync: unable to resolve file path for track {track}')
                continue
            if not file_path.exists():
                logger.error(f'sync: track file not found {file_path}')
                continue
            files_to_zip.append(file_path)

        n_files_written, dst_dir = self._write_export_zip(
            username=username,
            src_dir=src_dir,
            files_to_zip=files_to_zip,
            db_controller=None,
            job_id=None,
            extra_files=extra_files,
        )
        return n_files_written, dst_dir

    def get_user_music_root(self, username: str) -> Path:
        """The directory the export zip's entry names are relative to.

        Public because it is a contract, not an implementation detail: the Serato
        export tells the client where each track will be *inside the zip*, and the
        only way that answer can't drift from the zip itself is for both to be
        this one path (see _write_export_zip).
        """
        if '{user}' in self._serving_music_path_base:
            return Path(self._serving_music_path_base.format(user=username))
        return Path(self._serving_music_path_base) / username

    def _write_export_zip(
        self,
        username: str,
        src_dir: Path,
        files_to_zip: list[Path],
        db_controller: Optional[DbController],
        job_id: Optional[str],
        extra_files: Optional[list[Tuple[Path, str]]] = None,
    ) -> Tuple[int, Path]:
        dst_dir = Path(self._filebrowser_data_path_downloads.format(user=username)) / self._zip_name
        output_path = str(dst_dir.with_suffix('.zip'))
        datetime_start = datetime.datetime.now()
        n_files_written = 0
        with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for entry in files_to_zip:
                if not entry.is_file():
                    continue
                entry_to_write = Path(self._local_user_music_stem) / entry.relative_to(src_dir) if self._local_user_music_stem else entry.relative_to(src_dir)
                logger.info(f'exporting {entry} as {entry_to_write} for user {username}')
                zip_file.write(entry, entry_to_write)
                n_files_written += 1
                if db_controller and job_id:
                    datetime_now = datetime.datetime.now()
                    if (datetime_now - datetime_start).total_seconds() > self._update_job_period_s:
                        db_controller.update_export_job(job_id, n_files_written)
            for source, name_in_zip in extra_files or []:
                if not Path(source).is_file():
                    logger.error(f'export zip: extra file not found {source} for user {username}')
                    continue
                logger.info(f'exporting {source} as {name_in_zip} for user {username}')
                zip_file.write(source, name_in_zip)
                # Deliberately not counted in n_files_written: callers report that
                # as the track count (nTracksExported).
        return n_files_written, dst_dir

    def export_subsonic_music(self, db_config: dict, app_env: str, username: str, job_id: str) -> int:
        from pymix.factories.create_db_session import create_db_session
        session_factory = create_db_session(
            db_host=db_config["host"],
            db_port=db_config["port"],
        )
        db_controller = DbController(session_factory, app_env, 0)
        src_dir = self.get_user_music_root(username)
        files_to_zip = [entry for entry in src_dir.rglob('*') if entry.is_file()]
        n_files_written, _ = self._write_export_zip(
            username=username,
            src_dir=src_dir,
            files_to_zip=files_to_zip,
            db_controller=db_controller,
            job_id=job_id,
        )
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

    def snapshot_uploads(self, username: str) -> List[Path]:
        """Every file in ``uploads/{user}`` now, for :meth:`remove_fb_data_path` to clear later."""
        root = self.uploads_dir(username)
        return [f for f in root.rglob('*') if f.is_file()] if root.is_dir() else []

    def finish_upload_attempt(self, username: str, uploaded: List[Path], attempt: UploadAttempt) -> None:
        """
        Clear what an import started with, whatever its outcome (#38).

        ``uploads/`` used to be cleared only after a successful import, so a
        failed attempt's files, including ones that were never tagged, stayed
        for the next import to pick up. Never raises: a cleanup failure must not
        change the job's verdict.
        """
        try:
            self.remove_fb_data_path(username, only=uploaded)
        except Exception:
            logger.exception(f'failed to clear uploads for {username}')
        try:
            self._db_controller.clear_upload_attempt(username, attempt)
        except Exception:
            logger.exception(f'failed to clear the upload attempt for {username}')

    def remove_fb_data_path(self, username, watch: bool = False, only: Optional[List[Path]] = None):
        """
        Clear the user's filebrowser upload (or watch) directory.

        ``only`` limits it to the files an import saw when it started, from
        :meth:`snapshot_uploads`. The import clears ``uploads/`` whatever its
        outcome (#38). A file uploaded while the import ran belongs to the next
        attempt and must survive.
        """
        if watch:
            src_dir = Path(self._filebrowser_data_path_watch.format(user=username))
        else:
            src_dir = Path(self._filebrowser_data_path_uploads.format(user=username))
        if only is not None:
            logger.info(f'removing {len(only)} file(s) from {src_dir}')
            for filepath in only:
                filepath.unlink(missing_ok=True)
            # deepest first, so a parent is empty by the time it is reached
            for directory in sorted((d for d in src_dir.rglob('*') if d.is_dir()), reverse=True):
                if not any(directory.iterdir()):
                    directory.rmdir()
            return
        logger.info(f'removing contents of {src_dir}')
        for filepath in src_dir.iterdir():
            if filepath.is_dir():
                shutil.rmtree(filepath)
            else:
                filepath.unlink()
