from tinydb import TinyDB, Query
from tinydb.table import Document


class DbController:
    def __init__(self, db: TinyDB):
        self._db = db
        self._user_schema = ('username', 'password', 'beets_port', 'subsonic_port')

    def create_user(self, username: str, password: str):
        beets_port = self._get_available_beets_port()
        subsonic_port = self._get_available_subsonic_port()
        self._add_user(username, password, beets_port, subsonic_port)

    def _get_available_subsonic_port(self):
        try:
            last_subsonic_port = sorted(self._db.all(), key=lambda k: k['subsonic_port'])[-1]
        except IndexError:
            last_subsonic_port = 4553
        subsonic_port = last_subsonic_port + 1
        return subsonic_port

    def _get_available_beets_port(self):
        try:
            last_beets_port = sorted(self._db.all(), key=lambda k: k['beets_port'])[-1]
        except IndexError:
            last_beets_port = 8337
        beets_port = last_beets_port + 1
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
