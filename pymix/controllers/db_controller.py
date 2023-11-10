import uuid
import logging
from typing import Optional

from tinydb import TinyDB, Query
from tinydb.table import Document

from pymix.utils.get_available_port import get_available_port


logger = logging.getLogger(__name__)


class DbController:
    def __init__(self, db: TinyDB):
        self._db = db
        self._session_to_user_schema = ('session_id', 'user_id')
        self._user_schema = ('username', 'password', 'user_id', 'beets_port', 'subsonic_port', 'filebrowser_port')

    def create_user(self, username: str, password: str) -> str:
        User = Query()
        user_table = self._db.table('user_table')
        results = user_table.search(User.username == username)
        assert len(results) == 0, f'already have {len(results)} users with username {username}'
        beets_port = get_available_port()
        subsonic_port = get_available_port()
        filebrowser_port = get_available_port()
        user_id = uuid.uuid4().hex
        self._add_user(username, password, user_id, beets_port, subsonic_port, filebrowser_port)
        return self.create_session(username, password)

    def create_session(self, username: str, password: str) -> str:
        user = self.get_user(username)
        assert user['password'] == password
        user_id: str = user['user_id']
        SessionToUser = Query()
        session_table = self._db.table('session_table')
        results = session_table.search(SessionToUser.user_id == user_id)
        if len(results) == 1:
            logger.debug(f'already have a session for user {user_id}')
        elif len(results) > 1:
            msg = f'have {len(results)} in session table for user {user_id}'
            logger.error(msg)
            raise ValueError(msg)
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
            user_id: str,
            beets_port: int,
            subsonic_port: int,
            filebrowser_port: int
    ):
        user_table = self._db.table('user_table')
        user_table.insert(dict(zip(self._user_schema, (username, password, user_id, beets_port, subsonic_port, filebrowser_port))))

    def get_user_by_session_id(self, session_id: str) -> Optional[Document]:
        SessionToUser = Query()
        session_table = self._db.table('session_table')
        results = session_table.search(SessionToUser.session_id == session_id)
        if len(results) == 0:
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

    def get_total_number_of_users(self) -> int:
        return len(self._db)
