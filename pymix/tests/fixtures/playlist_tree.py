"""
The playlist tree's test harness (#202, #204): a real database (SQLite), the real
tree controller and Subsonic orchestrator, and a fake Navidrome that keeps playlists
the way it does.
"""
import asyncio
from pathlib import Path
from unittest import mock

import pytest
from pyrekordbox.rbxml import RekordboxXml
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from pymix.controllers.db_controller import DbController
from pymix.controllers.playlist_tree_controller import PlaylistTreeController
from pymix.controllers.rekordbox_xml_controller import RekordboxXMLController
from pymix.controllers.serato_controller import SeratoController
from pymix.model.db_tables import Base, PlaylistNodeRow, UserRow
from pymix.model.subboxplaylist import SubBoxPlaylist
from pymix.model.subboxtrack import SubBoxTrack
from pymix.orchestrators.subsonic_orchestrator import SubsonicOrchestrator
from pymix.services.tree_lock import TreeLocks

USER = {'username': 'dj', 'password': 'pw'}
MUSIC_ROOT = Path('/private-music/dj')


class FakeNavidrome:
    """The Subsonic calls an import makes, answered as Navidrome would."""

    def __init__(self):
        self.playlists: dict[str, SubBoxPlaylist] = {}
        self.entries: dict[str, list] = {}
        self.replaced: list[str] = []
        self.refuse_create = False
        # Names a rename is refused for, as Navidrome refusing an updatePlaylist.
        self.refuse_rename = set()
        self.renamed: list[tuple] = []
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

    async def create_playlist_from_ids(self, user, name, song_ids):
        return await self.create_playlist(user, name, [_track(s) for s in song_ids])

    async def replace_playlist(self, user, playlist_id, tracks):
        self.replaced.append(playlist_id)
        self.entries[playlist_id] = [t.sub_track_id for t in tracks if t.sub_track_id]
        return True

    async def get_playlist_tracks(self, user, playlist_id):
        return [_track(s) for s in self.entries[playlist_id]]

    async def rename_playlist(self, user, playlist_id, name):
        if name in self.refuse_rename:
            return False
        self.renamed.append((playlist_id, name))
        self.playlists[playlist_id].name = name
        return True


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
    # Under the music root, so the Serato export can place each one in the zip.
    return SubBoxTrack(name=song_id, artist='a', album='b', sub_track_id=song_id, subbox_id=f'sid-{song_id}',
                       pymix_path=MUSIC_ROOT / f'{song_id}.mp3')


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


# --- the exports (#204) ---------------------------------------------------------------

class XmlOrchestrator:
    """The Rekordbox orchestrator's part in an export, minus the files: a track is
    added to a playlist by its id and nothing else."""

    def create_xml(self, xml_path):
        return RekordboxXml(name='rekordbox', version='6.8.4', company='AlphaTheta')

    def add_track_to_rekordbox_playlist(self, rekordbox_xml, user_root, user, track, playlist, force=True):
        playlist.add_track(int(track.sub_track_id))

    def get_playlist(self, rekordbox_xml, name):
        return None

    def create_rekordbox_xml_playlist(self, rekordbox_xml, playlist):
        # A `none` user's playlist: the joined name, split, as the real one does.
        *folders, leaf = playlist.path_components or playlist.name.split(' / ')
        parent = rekordbox_xml.root_playlist_folder
        for folder in folders:
            parent = next((p for p in parent.get_playlists() if p.name == folder and p.is_folder), None) \
                or parent.add_playlist_folder(folder)
        return parent.add_playlist(leaf)

    def save_xml(self, rekordbox_xml, xml_output_path):
        self.saved = rekordbox_xml


@pytest.fixture
def rekordbox(tree, db_controller):
    controller = RekordboxXMLController.__new__(RekordboxXMLController)
    controller._playlist_tree = tree
    controller._subsonic_orchestrator = tree._subsonic
    controller._db_controller = db_controller
    controller._rekordbox_xml_orchestrator = XmlOrchestrator()

    async def no_tracks(user, page_size):
        return
        yield
    controller._subsonic_client = mock.Mock(get_all_tracks=no_tracks)
    return controller


@pytest.fixture
def serato(tree, db_controller):
    fb = mock.MagicMock()
    fb.get_user_music_root.return_value = MUSIC_ROOT
    return SeratoController(
        subsonic_orchestrator=tree._subsonic,
        serato_crate_orchestrator=mock.MagicMock(),
        serato_backup_file_handler=mock.MagicMock(),
        file_browser_file_handler=fb,
        rb_backup_file_handler=mock.MagicMock(),
        rb_xml_controller=mock.MagicMock(),
        db_controller=db_controller,
        wishlist_reconcile_service=mock.MagicMock(),
        serving_music_path_base='/private-music',
        beets_exec=mock.MagicMock(),
        playlist_tree_controller=tree,
    )


async def _xml(rekordbox, playlist_ids=None):
    """The exported XML's playlist tree as (indent + name, kind, track ids)."""
    # The unfiltered export pauses between pages of NOPLAYLIST tracks.
    with mock.patch('asyncio.sleep', mock.AsyncMock()):
        await rekordbox.create_rekordbox_xml_from_subsonic_playlists(
            '/root', USER, None, Path('out.xml'), playlist_ids=playlist_ids)
    lines = []

    def walk(node, depth):
        for child in node.get_playlists():
            if child.is_folder:
                lines.append(('  ' * depth + child.name, 'folder', None))
                walk(child, depth + 1)
            else:
                lines.append(('  ' * depth + child.name, 'playlist', child.get_tracks()))
    walk(rekordbox._rekordbox_xml_orchestrator.saved.root_playlist_folder, 0)
    return lines


def _playlists(lines):
    """Each playlist's full path and tracks, in order, from _xml's outline: what a
    Rekordbox import of the XML would read."""
    stack, playlists = [], []
    for name, kind, tracks in lines:
        depth = (len(name) - len(name.lstrip(' '))) // 2
        stack[depth:] = [name.strip()]
        if kind == 'playlist':
            playlists.append((list(stack), tracks))
    return playlists
