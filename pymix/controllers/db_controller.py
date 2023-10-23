from tinydb import TinyDB, Query
from tinydb.table import Document

from pymix.utils.get_available_port import get_available_port


class DbController:
    def __init__(self, db: TinyDB):
        self._db = db
        self._user_schema = ('username', 'password', 'beets_port', 'subsonic_port')

    def create_user(self, username: str, password: str):
        beets_port = self._get_available_beets_port()
        subsonic_port = self._get_available_subsonic_port()
        self._add_user(username, password, beets_port, subsonic_port)

    def _get_available_subsonic_port(self):
        subsonic_port = get_available_port()
        return subsonic_port

    def _get_available_beets_port(self):
        beets_port = get_available_port()
        return beets_port

    def _add_user(
            self,
            username: str,
            password: str,
            beets_port: int,
            subsonic_port: int
    ):
        self._db.insert(dict(zip(self._user_schema, (username, password, beets_port, subsonic_port))))

    def get_user(self, username: str) -> Document:
        User = Query()
        results = self._db.search(User.username == username)
        # todo be careful not to print passwords. Need to store password as hash really.
        assert len(results) == 1, f'found {len(results)} users with username {username}: {results}'
        result = results.pop()
        return result

    def delete_user(self, username: str):
        user = self.get_user(username)
        doc_id = user.doc_id
        self._db.remove(doc_ids=[doc_id])

    def get_total_number_of_users(self) -> int:
        return len(self._db)
