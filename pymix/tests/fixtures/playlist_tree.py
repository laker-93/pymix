"""
The playlist tree's test harness (#202, #204): a real database (SQLite), the real
tree controller and Subsonic orchestrator, and a fake Navidrome that keeps playlists
the way it does.
"""
import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from pymix.controllers.db_controller import DbController
from pymix.controllers.playlist_tree_controller import PlaylistTreeController
from pymix.model.db_tables import Base, PlaylistNodeRow, UserRow
from pymix.model.subboxplaylist import SubBoxPlaylist
from pymix.model.subboxtrack import SubBoxTrack
from pymix.orchestrators.subsonic_orchestrator import SubsonicOrchestrator
from pymix.services.tree_lock import TreeLocks

USER = {'username': 'dj', 'password': 'pw'}


class FakeNavidrome:
    """The Subsonic calls an import makes, answered as Navidrome would."""

    def __init__(self):
        self.playlists: dict[str, SubBoxPlaylist] = {}
        self.entries: dict[str, list] = {}
        self.replaced: list[str] = []
        self.refuse_create = False
        self.on_create = None
        self._next = 0

    def add(self, name, owner='dj', readonly=False, songs=('x',)):
        self._next += 1
        playlist_id = f'pl-{self._next}'
        self.playlists[playlist_id] = SubBoxPlaylist(
            name=name, subsonic_id=playlist_id, owner=owner, readonly=readonly, n_of_songs=len(songs))
        self.entries[playlist_id] = list(songs)
        return playlist_id

    async def get_playlists(self, user):
        return list(self.playlists.values())

    async def create_playlist(self, user, name, tracks):
        if self.refuse_create:
            return None
        playlist_id = self.add(name, songs=[t.sub_track_id for t in tracks if t.sub_track_id])
        if self.on_create:
            self.on_create(playlist_id)
        # Navidrome has it before the response gets back: anything else can run here.
        await asyncio.sleep(0)
        return playlist_id

    async def replace_playlist(self, user, playlist_id, tracks):
        self.replaced.append(playlist_id)
        self.entries[playlist_id] = [t.sub_track_id for t in tracks if t.sub_track_id]
        return True

    async def get_playlist_tracks(self, user, playlist_id):
        return [_track(s) for s in self.entries[playlist_id]]


@pytest.fixture
def sessions():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        session.add(UserRow(username='dj', password='pw', email='dj@example.com', user_id='user-1',
                            beets_port=1, subsonic_port=2, max_library_size=0))
        session.commit()
    return factory


@pytest.fixture
def db_controller(sessions, tmp_path):
    db = DbController(session_factory=sessions, app_env='test', max_library_size=0,
                      serving_music_path_base=str(tmp_path), staging_path=f'{tmp_path}/s/{{user}}/')
    db.set_playlist_tree_state('dj', 'live')
    return db


@pytest.fixture
def navidrome():
    return FakeNavidrome()


@pytest.fixture
def tree(sessions, db_controller, navidrome):
    locks = TreeLocks()
    orchestrator = SubsonicOrchestrator(navidrome, db_controller=db_controller, tree_locks=locks)
    return PlaylistTreeController(sessions, db_controller, orchestrator, locks)


def _track(song_id):
    return SubBoxTrack(name=song_id, artist='a', album='b', sub_track_id=song_id)


def _incoming(*path, songs=('s1', 's2')):
    return SubBoxPlaylist(
        name=' / '.join(path), path_components=list(path), tracks=[_track(s) for s in songs])


async def _import(tree, *playlists, origin='rekordbox', scan_finished=True):
    return await tree.import_playlists(USER, list(playlists), origin=origin, scan_finished=scan_finished)


async def _outline(tree):
    """Each live node as (indent + name, kind), in tree order."""
    body = await tree.get_tree(USER)
    depth = {}
    lines = []
    for node in body['nodes']:
        depth[node['node_id']] = depth.get(node['parent_id'], -1) + 1
        lines.append(('  ' * depth[node['node_id']] + node['name'], node['kind']))
    return lines


def _node(sessions, playlist_id):
    with sessions() as session:
        return session.query(PlaylistNodeRow).filter(PlaylistNodeRow.navidrome_playlist_id == playlist_id).one()


def _nodes(sessions):
    with sessions() as session:
        return session.query(PlaylistNodeRow).all()
