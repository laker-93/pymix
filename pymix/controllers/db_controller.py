import json
import uuid
import logging
import datetime
from pathlib import Path
from typing import Optional, Dict

from sqlalchemy.orm import sessionmaker

from pymix.model.db_tables import (
    UserRow, SessionRow, SubboxBeetsMapRow, LibraryRow,
    MetaHistoryRow, UserJobRow, JobRow, OriginalTrackMetaRow, UserTokenRow,
    PlaylistPathRow,
)
from pymix.model.original_track_meta import OriginalTracks
from pymix.utils.get_available_port import get_available_port


logger = logging.getLogger(__name__)


def _row_to_dict(row, exclude=('id',)):
    return {c.key: getattr(row, c.key) for c in row.__table__.columns if c.key not in exclude}


class DbController:
    def __init__(self, session_factory: sessionmaker, app_env: str, max_library_size: int):
        self._session_factory = session_factory
        self._app_env = app_env
        self._max_library_size = max_library_size

    def add_subbox_beet_map(self, username: str, subbox_id: str, beet_id: int) -> dict:
        try:
            user = self.get_user(username)
            user_id = user["user_id"]

            with self._session_factory() as session:
                existing = session.query(SubboxBeetsMapRow).filter(
                    SubboxBeetsMapRow.user_id == user_id,
                    SubboxBeetsMapRow.subbox_id == subbox_id,
                ).first()

                if existing:
                    raise ValueError(f"already got entry with subbox id {subbox_id}")

                new_record = SubboxBeetsMapRow(
                    user_id=user_id,
                    subbox_id=subbox_id,
                    beet_id=beet_id,
                    created_at=datetime.datetime.now().isoformat(),
                )
                session.add(new_record)
                session.commit()
                result = _row_to_dict(new_record)
                logger.info(f"Inserted new beet mapping for {username}: {subbox_id} → {beet_id}")
                return result

        except Exception as ex:
            logger.error(
                f"Error adding beet mapping for {username} (subbox_id={subbox_id}, beet_id={beet_id}): {repr(ex)}",
                exc_info=True
            )
            raise

    def get_subbox_beet_map(self, username: str, subbox_id: str) -> dict | None:
        try:
            user = self.get_user(username)
            user_id = user["user_id"]

            with self._session_factory() as session:
                row = session.query(SubboxBeetsMapRow).filter(
                    SubboxBeetsMapRow.user_id == user_id,
                    SubboxBeetsMapRow.subbox_id == subbox_id,
                ).first()

                if row:
                    logger.info(
                        f"Retrieved beet mapping for {username}: subbox_id={subbox_id} → beet_id={row.beet_id}"
                    )
                    return _row_to_dict(row)
                else:
                    logger.info(
                        f"No beet mapping found for {username} with subbox_id={subbox_id}"
                    )
                    return None

        except Exception as ex:
            logger.error(
                f"Error retrieving beet mapping for {username} (subbox_id={subbox_id}): {repr(ex)}",
                exc_info=True
            )
            raise

    def get_library_entry(
        self,
        username: str,
        subbox_id: str,
        version: Optional[int] = None
    ) -> Optional[Dict[str, any]]:
        try:
            user = self.get_user(username)
            user_id = user["user_id"]

            with self._session_factory() as session:
                query = session.query(LibraryRow).filter(
                    LibraryRow.user_id == user_id,
                    LibraryRow.subbox_id == subbox_id,
                )

                if version is not None:
                    query = query.filter(LibraryRow.version == version)
                    logger.info(f"Fetching metadata v{version} for subbox_id={subbox_id}, user={username}")
                else:
                    logger.info(f"Fetching latest metadata for subbox_id={subbox_id}, user={username}")

                results = query.all()
                if not results:
                    logger.warning(f"No metadata found for subbox_id={subbox_id}, user={username}")
                    return None
                assert len(results) == 1, f"have multiple entries with subbox id {subbox_id}."
                record = results[0]

                cuedata = record.cuedata
                if isinstance(cuedata, str):
                    try:
                        cuedata = json.loads(cuedata)
                    except json.JSONDecodeError:
                        logger.error(f"Invalid cuedata JSON for {subbox_id}, user={username}")
                        cuedata = {}

                return {
                    "subbox_id": record.subbox_id,
                    "username": username,
                    "version": record.version,
                    "source_app": record.source_app,
                    "updated_at": record.updated_at,
                    "cuedata": cuedata,
                }

        except Exception as ex:
            logger.error(
                f"Error retrieving metadata for subbox_id={subbox_id}, user={username}: {repr(ex)}",
                exc_info=True
            )
            raise

    def delete_track(
            self,
            username: str,
            subbox_id: str,
    ) -> Optional[bool]:
        try:
            user = self.get_user(username)
            user_id = user["user_id"]

            with self._session_factory() as session:
                # Delete from subbox_beets_map_table
                map_entry = session.query(SubboxBeetsMapRow).filter(
                    SubboxBeetsMapRow.user_id == user_id,
                    SubboxBeetsMapRow.subbox_id == subbox_id,
                ).first()
                if not map_entry:
                    logger.info(f"no entry found for user {username} with subbox id {subbox_id}")
                else:
                    session.delete(map_entry)
                    logger.info(f"removed {subbox_id} from subbox_beets_map_table")

                # Delete from original_track_meta_map_table
                meta_entry = session.query(OriginalTrackMetaRow).filter(
                    OriginalTrackMetaRow.user_id == user_id,
                    OriginalTrackMetaRow.subbox_id == subbox_id,
                ).first()
                if not meta_entry:
                    logger.info(f"no entry found in original track meta for user {username} with subbox id {subbox_id}")
                else:
                    session.delete(meta_entry)
                    logger.info(f"removed {subbox_id} from original_track_meta_map_table")

                # Delete from library_table
                lib_entry = session.query(LibraryRow).filter(
                    LibraryRow.user_id == user_id,
                    LibraryRow.subbox_id == subbox_id,
                ).first()
                if not lib_entry:
                    logger.info(f"no entry found in library table for user {username} with subbox id {subbox_id}")
                else:
                    session.delete(lib_entry)
                    logger.info(f"removed {subbox_id} from library_table")

                session.commit()
                return True

        except Exception as ex:
            logger.error(
                f"Error retrieving metadata for subbox_id={subbox_id}, user={username}: {repr(ex)}",
                exc_info=True
            )
            raise

    def save_original_track_meta(
            self,
            username: str,
            tracks: OriginalTracks,
    ):
        user = self.get_user(username)
        user_id = user["user_id"]

        with self._session_factory() as session:
            for track in tracks.tracks:
                assert track.subbox_id, f"no subbox id for track {track}"
                existing = session.query(OriginalTrackMetaRow).filter(
                    OriginalTrackMetaRow.user_id == user_id,
                    OriginalTrackMetaRow.subbox_id == track.subbox_id,
                ).first()

                if existing:
                    logger.info(f"have matching track for {track} in db")
                    continue

                new_entry = OriginalTrackMetaRow(
                    user_id=user_id,
                    user_location=track.userLocation,
                    staging_location=track.stagingLocation,
                    original_name=track.originalName,
                    original_artist=track.originalArtist,
                    original_album=track.originalAlbum,
                    subbox_id=track.subbox_id,
                )
                session.add(new_entry)
            session.commit()

    def get_meta_by_user_location(self, username: str, user_location: str) -> Optional[Dict]:
        user = self.get_user(username)
        user_id = user["user_id"]

        with self._session_factory() as session:
            results = session.query(OriginalTrackMetaRow).filter(
                OriginalTrackMetaRow.user_id == user_id,
                OriginalTrackMetaRow.user_location == user_location,
            ).all()

            if results:
                assert len(results) == 1, f"got multiple results for {username} {user_location}"
                return _row_to_dict(results[0])
            return None

    def update_metadata(
            self,
            username: str,
            subbox_id: str,
            cuedata: dict[str, any],
            source_app: str,
            change_type: str = "edit"
    ):
        user = self.get_user(username)
        user_id = user["user_id"]
        now = datetime.datetime.now().timestamp()

        with self._session_factory() as session:
            existing = session.query(LibraryRow).filter(
                LibraryRow.user_id == user_id,
                LibraryRow.subbox_id == subbox_id,
            ).first()

            if existing:
                version = existing.version + 1
                existing.cuedata = cuedata
                existing.source_app = source_app
                existing.updated_at = now
                existing.version = version
            else:
                version = 1
                new_entry = LibraryRow(
                    user_id=user_id,
                    subbox_id=subbox_id,
                    cuedata=cuedata,
                    source_app=source_app,
                    updated_at=now,
                    version=version,
                )
                session.add(new_entry)

            history_entry = MetaHistoryRow(
                user_id=user_id,
                subbox_id=subbox_id,
                version=version,
                cuedata=cuedata,
                source_app=source_app,
                change_type=change_type,
                changed_at=now,
            )
            session.add(history_entry)
            session.commit()

    def set_token(self, token: str):
        with self._session_factory() as session:
            session.add(UserTokenRow(user_id='', token=token))
            session.commit()

    def is_valid_token(self, token: str) -> bool:
        with self._session_factory() as session:
            count = session.query(UserTokenRow).filter(
                UserTokenRow.token == token,
                UserTokenRow.user_id == '',
            ).count()
            return count == 1

    def create_import_job(self, username: str, number_of_tracks_to_import: int, total_n_imported_tracks: int) -> str:
        user = self.get_user(username)
        user_id = user['user_id']
        job_id = uuid.uuid4().hex
        self._add_user_job(user_id, job_id)
        self._add_import_job(job_id, number_of_tracks_to_import, total_n_imported_tracks)
        return job_id

    def create_export_job(self, username: str, total_n_tracks: int) -> str:
        user = self.get_user(username)
        user_id = user['user_id']
        job_id = uuid.uuid4().hex
        self._add_user_job(user_id, job_id)
        self._add_export_job(job_id, total_n_tracks)
        return job_id

    def get_job_by_id(self, username: str, job_id: str) -> dict:
        user = self.get_user(username)
        user_id = user['user_id']

        with self._session_factory() as session:
            user_jobs = session.query(UserJobRow).filter(UserJobRow.user_id == user_id).all()
            job_ids = [uj.job_id for uj in user_jobs]
            assert job_id in job_ids, f'job id {job_id} not found in user job table for user {user_id}'

            job = session.query(JobRow).filter(JobRow.job_id == job_id).one()
            return _row_to_dict(job)

    def get_number_of_jobs(self, username: str, in_progress: bool) -> int:
        user = self.get_user(username)
        user_id = user['user_id']

        with self._session_factory() as session:
            user_jobs = session.query(UserJobRow).filter(UserJobRow.user_id == user_id).all()
            logger.info(f'have {len(user_jobs)} jobs for user id {user_id}')

            n_in_progress_jobs = 0
            for uj in user_jobs:
                count = session.query(JobRow).filter(
                    JobRow.job_id == uj.job_id,
                    JobRow.in_progress == in_progress,
                ).count()
                assert count <= 1, f'have {count} in progress? {in_progress} jobs for user {user_id}'
                n_in_progress_jobs += count

            assert n_in_progress_jobs <= 1, f'have {n_in_progress_jobs} in progress? {in_progress} jobs for user {user_id}'
            return n_in_progress_jobs

    def get_in_progress_job(self, username: str) -> dict:
        user = self.get_user(username)
        user_id = user['user_id']

        with self._session_factory() as session:
            user_jobs = session.query(UserJobRow).filter(UserJobRow.user_id == user_id).all()
            assert len(user_jobs) != 0, f'no entry found in user_job_table for user {user_id}'

            for uj in user_jobs:
                job = session.query(JobRow).filter(
                    JobRow.job_id == uj.job_id,
                    JobRow.in_progress == True,
                ).first()
                if job:
                    return _row_to_dict(job)

            raise AssertionError(f'no in-progress job found for user {user_id}')

    def update_export_job(self, job_id: str, n_exported_tracks: int):
        with self._session_factory() as session:
            job = session.query(JobRow).filter(JobRow.job_id == job_id).one()
            job.n_exported_tracks = n_exported_tracks
            session.commit()

    def job_completed(self, job_id: str, result: bool):
        with self._session_factory() as session:
            job = session.query(JobRow).filter(JobRow.job_id == job_id).one()
            job.in_progress = False
            job.result = result
            session.commit()

    def _add_import_job(self, job_id: str, number_of_tracks_to_import: int, total_n_imported_tracks: int):
        with self._session_factory() as session:
            session.add(JobRow(
                job_id=job_id,
                name='import',
                n_tracks_to_import=number_of_tracks_to_import,
                total_n_imported_tracks=total_n_imported_tracks,
                in_progress=True,
                result=None,
            ))
            session.commit()

    def _add_export_job(self, job_id: str, total_n_tracks: int):
        with self._session_factory() as session:
            session.add(JobRow(
                job_id=job_id,
                name='export',
                total_n_tracks_to_export=total_n_tracks,
                n_exported_tracks=0,
                in_progress=True,
                result=None,
            ))
            session.commit()

    def _add_user_job(self, user_id: str, job_id: str):
        with self._session_factory() as session:
            session.add(UserJobRow(user_id=user_id, job_id=job_id))
            session.commit()

    def create_user(self, username: str, password: str, email: str, token: str) -> str:
        with self._session_factory() as session:
            existing = session.query(UserRow).filter(UserRow.username == username).all()
            assert len(existing) == 0, f'already have {len(existing)} users with username {username}'

            beets_port = get_available_port()
            subsonic_port = get_available_port()
            user_id = uuid.uuid4().hex

            # Update the user_token table with the user_id
            token_row = session.query(UserTokenRow).filter(UserTokenRow.token == token).one()
            token_row.user_id = user_id

            session.add(UserRow(
                username=username,
                password=password,
                email=email,
                user_id=user_id,
                beets_port=beets_port,
                subsonic_port=subsonic_port,
                max_library_size=self._max_library_size,
            ))
            session.commit()

        return self.create_session(username, password)

    def create_session(self, username: str, password: str) -> str:
        user = self.get_user(username)
        assert user['password'] == password
        user_id = user['user_id']

        with self._session_factory() as session:
            results = session.query(SessionRow).filter(SessionRow.user_id == user_id).all()
            if len(results) == 1:
                logger.info(f'already have a session for user {user_id}')
                session_id = results[0].session_id
            elif len(results) > 1:
                msg = f'have {len(results)} in session table for user {user_id}'
                logger.error(msg)
                raise ValueError(msg)
            else:
                session_id = uuid.uuid4().hex
                session.add(SessionRow(session_id=session_id, user_id=user_id))
                session.commit()
            return session_id

    def get_user_by_session_id(self, session_id: str) -> Optional[dict]:
        logger.debug(f'get user by session id {session_id}')

        with self._session_factory() as session:
            results = session.query(SessionRow).filter(SessionRow.session_id == session_id).all()
            if len(results) == 0:
                logger.error(f'no results found in session table for session id {session_id}')
                return None
            if len(results) > 1:
                msg = f'error found {len(results)} sessions associated with session id {session_id}'
                logger.error(msg)
                raise ValueError(msg)

            user_id = results[0].user_id
            user_results = session.query(UserRow).filter(UserRow.user_id == user_id).all()
            assert len(user_results) == 1, f'found {len(user_results)} users in user table with user id {user_id}'
            result = _row_to_dict(user_results[0])
            logger.debug(f'returning a single user table result for session id {session_id}')
            return result

    def get_user(self, username: str) -> dict:
        with self._session_factory() as session:
            results = session.query(UserRow).filter(UserRow.username == username).all()
            assert len(results) == 1, f'found {len(results)} users with username {username}'
            return _row_to_dict(results[0])

    def delete_user(self, username: str):
        with self._session_factory() as session:
            user = session.query(UserRow).filter(UserRow.username == username).one()
            session.delete(user)
            session.commit()

    def delete_session(self, session_id: str):
        with self._session_factory() as session:
            row = session.query(SessionRow).filter(SessionRow.session_id == session_id).one()
            session.delete(row)
            session.commit()

    def get_total_number_of_users(self) -> int:
        with self._session_factory() as session:
            return session.query(UserRow).count()

    def user_library_size_exceeded(self, username: str, size_import: int) -> tuple[bool, int, int]:
        total_size = 0
        for file in Path(f'/private-music/{username}').rglob('*'):
            try:
                total_size += file.stat().st_size
            except FileNotFoundError:
                # file disappeared between rglob and stat
                logger.error(f"Missing during scan: {file} for user {username}")
                continue
        user = self.get_user(username)
        max_storage_bytes = int(user['max_library_size'])
        if int(total_size + size_import) > max_storage_bytes:
            logger.error(
                f"user {username} has exceeded max size of library of {user['max_library_size']} with current size: {total_size} and attempted import {size_import}"
            )
            return True, max_storage_bytes, total_size
        else:
            return False, max_storage_bytes, total_size

    def save_playlist_paths(self, username: str, playlists: list[dict]):
        """Store display_name -> path_components mappings for a user's playlists."""
        user = self.get_user(username)
        user_id = user['user_id']
        with self._session_factory() as session:
            for pl in playlists:
                existing = session.query(PlaylistPathRow).filter(
                    PlaylistPathRow.user_id == user_id,
                    PlaylistPathRow.display_name == pl['display_name'],
                ).first()
                if existing:
                    existing.path_components = pl['path_components']
                else:
                    session.add(PlaylistPathRow(
                        user_id=user_id,
                        display_name=pl['display_name'],
                        path_components=pl['path_components'],
                    ))
            session.commit()

    def get_playlist_paths(self, username: str) -> list[dict]:
        """Return all playlist path mappings for a user."""
        user = self.get_user(username)
        user_id = user['user_id']
        with self._session_factory() as session:
            rows = session.query(PlaylistPathRow).filter(
                PlaylistPathRow.user_id == user_id,
            ).all()
            return [_row_to_dict(r) for r in rows]
