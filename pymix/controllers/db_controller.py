import uuid
import logging
from typing import Optional

from tinydb import TinyDB, Query
from tinydb.table import Document

from pymix.utils.get_available_port import get_available_port


logger = logging.getLogger(__name__)


class DbController:
    def __init__(self, db: TinyDB, app_env: str):
        self._db = db
        self._app_env = app_env
        self._session_to_user_schema = ('session_id', 'user_id')
        self._user_schema = ('username', 'password', 'email', 'user_id', 'beets_port', 'subsonic_port')
        self._user_jobs_schema = ('user_id', 'job_id')
        self._import_job_schema = ('job_id', 'name', 'n_tracks_to_import', 'total_n_imported_tracks', 'in_progress', 'result')
        self._export_job_schema = ('job_id', 'name', 'total_n_tracks_to_export', 'n_exported_tracks', 'in_progress', 'result')

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

    def create_user(self, username: str, password: str, email: str) -> str:
        User = Query()
        user_table = self._db.table('user_table')
        results = user_table.search(User.username == username)
        assert len(results) == 0, f'already have {len(results)} users with username {username}'
        beets_port = get_available_port()
        subsonic_port = get_available_port()
        user_id = uuid.uuid4().hex
        self._add_user(username, password, email, user_id, beets_port, subsonic_port)
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
    ):
        user_table = self._db.table('user_table')
        user_table.insert(dict(zip(self._user_schema, (username, password, email, user_id, beets_port, subsonic_port))))

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
