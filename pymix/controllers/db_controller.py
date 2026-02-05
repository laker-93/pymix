import json
import uuid
import logging
import datetime
from pathlib import Path
from typing import Optional, Dict
from zoneinfo import ZoneInfo

from tinydb import TinyDB, Query
from tinydb.table import Document

from pymix.model.original_track_meta import OriginalTracks
from pymix.utils.get_available_port import get_available_port


logger = logging.getLogger(__name__)


class DbController:
    def __init__(self, db: TinyDB, app_env: str, max_library_size: int):
        self._db = db
        self._app_env = app_env
        self._session_to_user_schema = ('session_id', 'user_id')
        self._user_schema = ('username', 'password', 'email', 'user_id', 'beets_port', 'subsonic_port', 'max_library_size')
        self._subbox_beets_map_schema = (
            'user_id',  # FK -> users.user_id
            'subbox_id',  # unique per user (primary identifier for track)
            'beet_id',  # unique per user
            "created_at"
        )
        self._library_schema = (
            'user_id',  # FK -> users.user_id
            'subbox_id',  # unique per user (primary identifier for track)
            'cuedata',
            'source_app',
            'updated_at',
            'version'
        )
        self._meta_history_schema = (
            'user_id',  # FK -> users.user_id
            'subbox_id',  # FK -> library.subbox_id
            'version',  # int
            'hash',  # checksum for diff
            'cuedata',  # the JSON snapshot of that version
            'source_app',  # where update came from
            'change_type',  # 'upload', 'edit', 'sync', 'merge'
            'changed_at',  # ISO timestamp
        )
        self._user_jobs_schema = ('user_id', 'job_id')
        self._import_job_schema = ('job_id', 'name', 'n_tracks_to_import', 'total_n_imported_tracks', 'in_progress', 'result')
        self._export_job_schema = ('job_id', 'name', 'total_n_tracks_to_export', 'n_exported_tracks', 'in_progress', 'result')
        self._user_token_schema = ('user_id', 'token')
        self._original_track_meta = (
            'user_id',
            'subbox_id',
            'user_location',
            'staging_location',
            'original_name',
            'original_artist',
            'original_album'

        )
        self._max_library_size = max_library_size

    def add_subbox_beet_map(self, username: str, subbox_id: str, beet_id: int) -> dict:
        """
        Adds or updates a mapping between subbox_id and beet_id for a specific user.
        If the mapping already exists, it will be updated.
        Returns the inserted or updated record.
        """
        try:
            # 1️⃣ Get user_id
            user = self.get_user(username)
            user_id = user["user_id"]

            Map = Query()
            query = (Map.user_id == user_id) & (Map.subbox_id == subbox_id)

            table = self._db.table('subbox_beets_map_table')
            results = table.search(query)

            if results:
                raise ValueError(f"already got entry {results} with subbox id {subbox_id}")
            else:
                # 3️⃣ Insert new mapping
                new_record = {
                    "user_id": user_id,
                    "subbox_id": subbox_id,
                    "beet_id": beet_id,
                    "created_at": datetime.datetime.now().isoformat()
                }
                table.insert(new_record)
                logger.info(f"Inserted new beet mapping for {username}: {subbox_id} → {beet_id}")
                result = new_record
            return result

        except Exception as ex:
            logger.error(
                f"Error adding beet mapping for {username} (subbox_id={subbox_id}, beet_id={beet_id}): {repr(ex)}",
                exc_info=True
            )
            raise

    def get_subbox_beet_map(self, username: str, subbox_id: str) -> dict | None:
        """
        Retrieves a mapping between subbox_id and beet_id for a specific user.
        Returns the record if found, otherwise None.
        """
        try:
            # 1️⃣ Get user_id
            user = self.get_user(username)
            user_id = user["user_id"]

            Map = Query()
            query = (Map.user_id == user_id) & (Map.subbox_id == subbox_id)

            table = self._db.table('subbox_beets_map_table')
            result = table.get(query)

            if result:
                logger.info(
                    f"Retrieved beet mapping for {username}: subbox_id={subbox_id} → beet_id={result.get('beet_id')}"
                )
            else:
                logger.info(
                    f"No beet mapping found for {username} with subbox_id={subbox_id}"
                )

            return result

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
        """
        Retrieve metadata for a given user's track.
        If version is provided, look for that specific version (if stored).
        Otherwise, return the latest record for that track.
        """

        try:
            # --- 1️⃣ Resolve user_id ---
            user = self.get_user(username)
            user_id = user["user_id"]

            # --- 2️⃣ Build query ---
            Library = Query()
            query = (Library.user_id == user_id) & (Library.subbox_id == subbox_id)

            if version is not None:
                query = query & (Library.version == version)
                logger.info(f"Fetching metadata v{version} for subbox_id={subbox_id}, user={username}")
            else:
                logger.info(f"Fetching latest metadata for subbox_id={subbox_id}, user={username}")

            library_table = self._db.table('library_table')
            results = library_table.search(query)
            if not results:
                logger.warning(f"No metadata found for subbox_id={subbox_id}, user={username}")
                return None
            assert len(results) == 1, f"have multiple entries with subbox id {subbox_id}. results: {results}."
            record = results.pop()
            # --- 3️⃣ Parse JSON if needed ---
            cuedata = record.get("cuedata")
            if isinstance(cuedata, str):
                try:
                    cuedata = json.loads(cuedata)
                except json.JSONDecodeError:
                    logger.error(f"Invalid cuedata JSON for {subbox_id}, user={username}")
                    cuedata = {}

            # --- 4️⃣ Return normalized record ---
            return {
                "subbox_id": record.get("subbox_id"),
                "username": username,
                "version": record.get("version"),
                "source_app": record.get("source_app"),
                "updated_at": record.get("updated_at"),
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
        """
        Delete metadata for a track in the db.
        TODO - set an is_active flag to false instead of deleting permanently.
        """

        try:
            user = self.get_user(username)
            user_id = user["user_id"]

            Map = Query()
            query = (Map.user_id == user_id) & (Map.subbox_id == subbox_id)

            table = self._db.table('subbox_beets_map_table')
            results = table.search(query)

            if not results:
                logger.info(f"no entry found for user {username} with subbox id {subbox_id}")
            else:
                assert len(results) == 1, f"got {len(results)} for {username} with subbox id {subbox_id}. Results: {results}"
                result = results[0]
                table.remove(doc_ids=[result.doc_id])
                logger.info(f"removed {subbox_id} from subbox_beets_map_table")

            OriginalMetaMap = Query()
            query = (OriginalMetaMap.user_id == user_id) & (OriginalMetaMap.subbox_id == subbox_id)

            table = self._db.table('original_track_meta_map_table')
            results = table.search(query)

            if not results:
                logger.info(f"no entry found in original track meta for user {username} with subbox id {subbox_id}")
            else:
                assert len(results) == 1, f"got {len(results)} for {username} with subbox id {subbox_id}. Results: {results}"
                result = results[0]
                table.remove(doc_ids=[result.doc_id])
                logger.info(f"removed {subbox_id} from original_track_meta_map_table")

            table = self._db.table('library_table')
            Track = Query()
            track_query = (Track.user_id == user_id) & (Track.subbox_id == subbox_id)
            results = table.search(track_query)
            if not results:
                logger.info(f"no entry found in library table for user {username} with subbox id {subbox_id}")
            else:
                assert len(results) == 1, f"got {len(results)} for {username} with subbox id {subbox_id}. Results: {results}"
                result = results[0]
                table.remove(doc_ids=[result.doc_id])
                logger.info(f"removed {subbox_id} from library_table")

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

        # Check no existing meta
        table = self._db.table('original_track_meta_map_table')
        for track in tracks.tracks:
            Track = Query()
            assert track.subbox_id, f"no subbox id for track {track}"
            track_query = (Track.user_id == user_id) & (Track.subbox_id == track.subbox_id)
            results = table.search(track_query)
            if results:
                existing = results.pop()
                # this can happen if a previous attempt failed to import successfully.
                logger.info(f"have matching track for {track} in db with: {existing}")
                continue

            # Update or insert in library
            new_entry = {
                "user_id": user_id,
                "user_location": track.userLocation,
                "staging_location": track.stagingLocation,
                "original_name": track.originalName,
                "original_artist": track.originalArtist,
                "original_album": track.originalAlbum,
                "subbox_id": track.subbox_id,
            }

            table.insert(new_entry)

    def get_meta_by_user_location(self, username: str, user_location: str) -> Optional[Dict]:
        user = self.get_user(username)
        user_id = user["user_id"]
        self._db.clear_cache()
        table = self._db.table('original_track_meta_map_table')
        query = Query()
        results = table.search((query.user_id == user_id) & (query.user_location == user_location))
        if results:
            assert len(results) == 1, f"got multiple results for {username} {user_location}: {results}"
            result = results.pop()
        else:
            result = None
        return result


    def update_metadata(
            self,
            username: str,
            subbox_id: str,
            cuedata: dict[str, any],
            source_app: str,
            change_type: str = "edit"  # upload, edit, sync, merge
    ):
        user = self.get_user(username)
        user_id = user["user_id"]
        now = datetime.datetime.now().timestamp()

        # Fetch existing track
        library_table = self._db.table('library_table')
        Track = Query()
        track_query = (Track.user_id == user_id) & (Track.subbox_id == subbox_id)
        results = library_table.search(track_query)
        if results:
            existing = results.pop()
            version = existing["version"] + 1
        else:
            version = 1

        # Update or insert in library
        new_entry = {
            "user_id": user_id,
            "subbox_id": subbox_id,
            "cuedata": cuedata,
            "source_app": source_app,
            "updated_at": now,
            "version": version,
        }

        library_table.upsert(new_entry, track_query)

        # Log in meta_history
        meta_history_table = self._db.table('meta_history_table')
        meta_history_table.insert({
            "user_id": user_id,
            "subbox_id": subbox_id,
            "version": version,
            "cuedata": cuedata,
            "source_app": source_app,
            "change_type": change_type,
            "changed_at": now
        })

    def set_token(self, token: str):
        user_token_table = self._db.table('user_token_table')
        user_token_table.insert({'user_id': '', 'token': token})

    def is_valid_token(self, token: str) -> bool:
        self._db.clear_cache()
        user_token_table = self._db.table('user_token_table')
        Token = Query()
        results = user_token_table.search((Token.token == token) & (Token.user_id == ""))
        return len(results) == 1

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

    def get_job_by_id(self, username: str, job_id: str) -> int:
        self._db.clear_cache()
        user = self.get_user(username)
        user_id: str = user['user_id']
        user_job_table = self._db.table('user_job_table')
        UserJob = Query()
        logger.debug(f'getting in progress jobs for user id {user_id}')
        results = user_job_table.search(UserJob.user_id == user_id)
        assert job_id in map(lambda x: x['job_id'], results), f'job id {job_id} not found in user job table for user {user_id}'

        Job = Query()
        job_table = self._db.table('job_table')
        results = job_table.search(Job.job_id == job_id)
        assert len(results) == 1, f'found {len(results)} jobs for job id {job_id}'
        return results.pop()


    def get_number_of_jobs(self, username: str, in_progress: bool) -> int:
        self._db.clear_cache()
        user = self.get_user(username)
        user_id: str = user['user_id']
        user_job_table = self._db.table('user_job_table')
        UserJob = Query()
        logger.info(f'getting in progress jobs for user id {user_id}')
        results = user_job_table.search(UserJob.user_id == user_id)
        job_table = self._db.table('job_table')
        n_in_progress_jobs = 0
        logger.debug(f'job table content {job_table.all()}')
        logger.info(f'have {len(results)} jobs for user id {user_id}')
        for result in results:
            Job = Query()
            job_id: str = result['job_id']
            job_results = job_table.search((Job.job_id == job_id) & (Job.in_progress == in_progress))
            assert len(job_results) == 1 or len(job_results) == 0, f'have {len(job_results)} in progress? {in_progress} jobs for user {user_id}'
            n_in_progress_jobs += len(job_results)
        assert n_in_progress_jobs == 1 or n_in_progress_jobs == 0, f'have {n_in_progress_jobs} in progress? {in_progress} jobs for user {user_id}'
        return n_in_progress_jobs

    def get_in_progress_job(self, username: str) -> Document:
        user = self.get_user(username)
        user_id: str = user['user_id']
        user_job_table = self._db.table('user_job_table')
        UserJob = Query()
        results = user_job_table.search(UserJob.user_id == user_id)

        assert len(results) != 0, f'no entry found in user_job_table for user {user_id}'
        job_table = self._db.table('job_table')
        job_table.clear_cache()
        job = None
        for result in results:
            Job = Query()
            job_id: str = result['job_id']
            job_results = job_table.search((Job.job_id == job_id) & (Job.in_progress==True))
            if len(job_results) == 1:
                job = job_results.pop()
                break
        assert job
        return job


    def update_export_job(self, job_id: str, n_exported_tracks: int):
        job_table = self._db.table('job_table')
        ExportJob = Query()
        job_results = job_table.search(ExportJob.job_id == job_id)
        assert len(job_results) == 1, f'job results: {job_results}'
        job_table.update({'n_exported_tracks': n_exported_tracks}, ExportJob.job_id == job_id)

    def job_completed(self, job_id: str, result: bool):
        job_table = self._db.table('job_table')
        Job = Query()
        job_table.upsert({'in_progress': False, 'result': result}, Job.job_id == job_id)

    def _add_import_job(self, job_id: str, number_of_tracks_to_import: int, total_n_imported_tracks):
        job_table = self._db.table('job_table')
        job_table.insert(dict(zip(self._import_job_schema, (job_id, 'import', number_of_tracks_to_import, total_n_imported_tracks, True, None))))

    def _add_export_job(self, job_id: str, total_n_tracks):
        job_table = self._db.table('job_table')
        job_table.insert(dict(zip(self._export_job_schema, (job_id, 'export', total_n_tracks, 0, True, None))))

    def _add_user_job(self, user_id: str, job_id: str):
        user_job_table = self._db.table('user_job_table')
        user_job_table.insert(dict(zip(self._user_jobs_schema, (user_id, job_id))))

    def create_user(self, username: str, password: str, email: str, token: str) -> str:
        User = Query()
        user_table = self._db.table('user_table')
        results = user_table.search(User.username == username)
        assert len(results) == 0, f'already have {len(results)} users with username {username}'

        beets_port = get_available_port()
        subsonic_port = get_available_port()
        user_id = uuid.uuid4().hex
        # Update the user_token table with the user_id
        user_token_table = self._db.table('user_token_table')
        Token = Query()
        token_results = user_token_table.search(Token.token == token)
        assert len(token_results) == 1, f'token {token} not found in user_token_table'
        user_token_table.update({'user_id': user_id}, Token.token == token)

        self._add_user(username, password, email, user_id, beets_port, subsonic_port, self._max_library_size)
        return self.create_session(username, password)

    def create_session(self, username: str, password: str) -> str:
        user = self.get_user(username)
        assert user['password'] == password
        user_id: str = user['user_id']
        SessionToUser = Query()
        session_table = self._db.table('session_table')
        results = session_table.search(SessionToUser.user_id == user_id)
        if len(results) == 1:
            logger.info(f'already have a session for user {user_id}')
            session_id = results[0]['session_id']
        elif len(results) > 1:
            msg = f'have {len(results)} in session table for user {user_id}'
            logger.error(msg)
            raise ValueError(msg)
        else:
            session_id = uuid.uuid4().hex
            self._add_session(session_id, user_id)
        return session_id

    def _add_session(
            self,
            session_id: str,
            user_id: str
    ):
        session_table = self._db.table('session_table')
        session_table.insert(dict(zip(self._session_to_user_schema, (session_id, user_id))))

    def _add_user(
            self,
            username: str,
            password: str,
            email: str,
            user_id: str,
            beets_port: int,
            subsonic_port: int,
            max_library_size: int,
    ):
        user_table = self._db.table('user_table')
        user_table.insert(dict(zip(self._user_schema, (username, password, email, user_id, beets_port, subsonic_port, max_library_size))))

    def get_user_by_session_id(self, session_id: str) -> Optional[Document]:
        logger.debug(f'get user by session id {session_id}')
        SessionToUser = Query()
        session_table = self._db.table('session_table')
        results = session_table.search(SessionToUser.session_id == session_id)
        if len(results) == 0:
            logger.error(f'no results found in session table for session id {session_id}')
            return None
        if len(results) > 1:
            msg = f'error found {len(results)} sessions associated with session id {session_id}'
            logger.error(msg)
            raise ValueError(msg)
        if len(results) == 1:
            result = results.pop()
            user_id: str = result['user_id']
            User = Query()
            user_table = self._db.table('user_table')
            results = user_table.search(User.user_id == user_id)
            assert len(results) == 1, f'found {len(results)} users in user table with user id {user_id}'
            result = results.pop()
            logger.debug(f'returning a single user table result for session id {session_id}')
            return result

    def get_user(self, username: str) -> Document:
        User = Query()
        user_table = self._db.table('user_table')
        results = user_table.search(User.username == username)
        # todo be careful not to print passwords. Need to store password as hash really.
        assert len(results) == 1, f'found {len(results)} users with username {username}: {results}'
        result = results.pop()
        return result

    def delete_user(self, username: str):
        user = self.get_user(username)
        doc_id = user.doc_id
        user_table = self._db.table('user_table')
        user_table.remove(doc_ids=[doc_id])

    def delete_session(self, session_id: str):
        Session = Query()
        session_table = self._db.table('session_table')
        results = session_table.search(Session.session_id == session_id)
        result = results.pop()
        doc_id = result.doc_id
        session_table.remove(doc_ids=[doc_id])

    def get_total_number_of_users(self) -> int:
        self._db.clear_cache()
        user_table = self._db.table('user_table')
        return len(user_table)

    def user_library_size_exceeded(self, username: str, size_import: int) -> int:
        self._db.clear_cache()
        total_size = sum(file.stat().st_size for file in Path(f'/private-music/{username}').rglob('*'))
        user = self.get_user(username)
        if int(total_size + size_import) > int(user['max_library_size']):
            logger.error(
                f"user {username} has exceeded max size of library of {user['max_library_size']} with current size: {total_size} and attempted import {size_import}"
            )
            return True
        else:
            return False
