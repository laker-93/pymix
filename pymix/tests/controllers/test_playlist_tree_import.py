"""
#202: a Rekordbox or Serato import builds a `live` user's playlist tree, and a
re-import matches by `source_path`, so a playlist the user moved or renamed in subbox
is updated in place rather than duplicated (design-playlists-and-undo §5.1, §5.2).

The database is real (SQLite), and so are the tree controller and the Subsonic
orchestrator; Navidrome is a fake that keeps playlists the way it does.
"""
import asyncio
import logging
from unittest import mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from pymix.controllers.db_controller import DbController
from pymix.controllers.playlist_tree_controller import PlaylistTreeController
from pymix.controllers.rekordbox_xml_controller import RekordboxXMLController
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


def _incoming(*path, songs=('s1', 's2')):
    return SubBoxPlaylist(
        name=' / '.join(path), path_components=list(path),
        tracks=[SubBoxTrack(name=s, artist='a', album='b', sub_track_id=s) for s in songs],
    )


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


# --- a first import builds the tree ------------------------------------------------

@pytest.mark.anyio
async def test_a_nested_rekordbox_import_mirrors_the_source(tree, navidrome, sessions):
    report = await _import(
        tree,
        _incoming('House', '2024', 'Deep'),
        _incoming('House', '2024', 'Tech'),
        _incoming('House', 'Warmup'),
        _incoming('Loose'),
        _incoming('Techno', 'Peak'),
    )

    assert await _outline(tree) == [
        ('House', 'folder'),
        ('  2024', 'folder'),
        ('    Deep', 'playlist'),
        ('    Tech', 'playlist'),
        ('  Warmup', 'playlist'),
        ('Loose', 'playlist'),
        ('Techno', 'folder'),
        ('  Peak', 'playlist'),
    ]
    # Navidrome gets the leaf name; the path is the tree's.
    assert sorted(p.name for p in navidrome.playlists.values()) == ['Deep', 'Loose', 'Peak', 'Tech', 'Warmup']
    # The report names what was imported, as the source library calls it.
    assert report.created == ['House / 2024 / Deep', 'House / 2024 / Tech', 'House / Warmup', 'Loose', 'Techno / Peak']
    by_path = {tuple(n.source_path): n for n in _nodes(sessions)}
    assert set(by_path) == {
        ('House',), ('House', '2024'), ('House', '2024', 'Deep'), ('House', '2024', 'Tech'),
        ('House', 'Warmup'), ('Loose',), ('Techno',), ('Techno', 'Peak'),
    }
    assert {n.origin for n in by_path.values()} == {'rekordbox'}


@pytest.mark.anyio
async def test_a_none_user_gets_joined_names_and_no_nodes(tree, navidrome, sessions, db_controller):
    db_controller.set_playlist_tree_state('dj', 'none')

    report = await _import(tree, _incoming('House', 'Deep'), _incoming('Loose'))

    assert sorted(p.name for p in navidrome.playlists.values()) == ['House / Deep', 'Loose']
    assert report.created == ['House / Deep', 'Loose']
    assert _nodes(sessions) == []


# --- a re-import updates in place, wherever the playlist now is ---------------------

@pytest.mark.anyio
async def test_a_moved_and_renamed_playlist_is_updated_in_place(tree, navidrome, sessions):
    await _import(tree, _incoming('House', 'Deep'), _incoming('House', 'Tech'))
    deep = next(pid for pid, p in navidrome.playlists.items() if p.name == 'Deep')
    await tree.move_node(USER, _node(sessions, deep).node_id, None, 0)
    navidrome.playlists[deep].name = 'Deep (Sunday)'
    before = await _outline(tree)

    report = await _import(tree, _incoming('House', 'Deep', songs=('s3',)), _incoming('House', 'Tech'))

    assert report.created == []
    assert report.updated == ['House / Deep', 'House / Tech']
    assert navidrome.entries[deep] == ['s3']
    assert len(navidrome.playlists) == 2
    # Its name and its place are the user's, and stay as they left them.
    assert await _outline(tree) == before
    assert before[0] == ('Deep (Sunday)', 'playlist')


@pytest.mark.anyio
async def test_a_new_playlist_lands_in_its_folder_where_the_user_moved_it(tree, navidrome, sessions):
    await _import(tree, _incoming('Sets', 'House', 'Deep'), _incoming('Loose'))
    house = next(n for n in _nodes(sessions) if n.source_path == ['Sets', 'House'])
    await tree.move_node(USER, house.node_id, None, None)

    await _import(tree, _incoming('Sets', 'House', 'Deep'), _incoming('Sets', 'House', 'Tech'), _incoming('Loose'))

    assert await _outline(tree) == [
        ('Sets', 'folder'),
        ('Loose', 'playlist'),
        ('House', 'folder'),
        ('  Deep', 'playlist'),
        ('  Tech', 'playlist'),
    ]


@pytest.mark.anyio
async def test_a_folder_the_user_made_is_never_taken_over(tree, navidrome, sessions):
    await tree.create_node(USER, 'folder', name='House')

    await _import(tree, _incoming('House', 'Deep'))

    assert await _outline(tree) == [
        ('House', 'folder'),
        ('House', 'folder'),
        ('  Deep', 'playlist'),
    ]


@pytest.mark.anyio
async def test_a_playlist_adopted_from_navidrome_is_not_matched(tree, navidrome, sessions):
    """Made in Feishin's own modal, so no source_path: a Rekordbox playlist of the
    same name sits beside it rather than overwriting it."""
    mine = navidrome.add('Deep')

    await _import(tree, _incoming('Deep'))

    assert navidrome.replaced == []
    assert navidrome.entries[mine] == ['x']
    assert [name for name, _ in await _outline(tree)] == ['Deep', 'Deep']


@pytest.mark.anyio
async def test_a_smart_playlist_is_never_written_over(tree, navidrome, sessions):
    await _import(tree, _incoming('Deep'))
    [deep] = navidrome.playlists
    navidrome.playlists[deep].readonly = True

    report = await _import(tree, _incoming('Deep'))

    assert navidrome.replaced == []
    assert len(report.created) == 1
    assert len(navidrome.playlists) == 2


@pytest.mark.anyio
async def test_two_nodes_with_one_source_path_are_each_updated_once(tree, navidrome, sessions, caplog):
    """Rekordbox allows sibling duplicates. The first incoming updates the first in
    tree order; the second, the second; and the ambiguity is logged."""
    await _import(tree, _incoming('House', 'Deep', songs=('a',)), _incoming('House', 'Deep', songs=('b',)))
    first, second = (n['navidrome_playlist_id'] for n in (await tree.get_tree(USER))['nodes'][1:])

    with caplog.at_level(logging.WARNING):
        report = await _import(tree, _incoming('House', 'Deep', songs=('c',)), _incoming('House', 'Deep', songs=('d',)))

    assert report.created == []
    assert navidrome.replaced == [first, second]
    assert (navidrome.entries[first], navidrome.entries[second]) == (['c'], ['d'])
    assert "2 playlists imported as 'House / Deep'" in caplog.text


@pytest.mark.anyio
async def test_a_playlist_moved_out_of_a_trashed_folder_path_is_still_found(tree, navidrome, sessions):
    """The folder it came from was deleted outright: the playlist is still matched
    by its own path, and the folder is only recreated for a new playlist."""
    await _import(tree, _incoming('House', 'Deep'))
    deep = next(iter(navidrome.playlists))
    await tree.move_node(USER, _node(sessions, deep).node_id, None, None)
    with sessions() as session:
        session.query(PlaylistNodeRow).filter(PlaylistNodeRow.kind == 'folder').delete()
        session.commit()

    report = await _import(tree, _incoming('House', 'Deep'))

    assert report.updated == ['House / Deep']
    assert await _outline(tree) == [('Deep', 'playlist')]


# --- the scan, failures and the lock -------------------------------------------------

@pytest.mark.anyio
async def test_an_unfinished_scan_holds_back_matches_but_still_builds_new_ones(tree, navidrome, sessions):
    await _import(tree, _incoming('House', 'Deep'))

    report = await _import(tree, _incoming('House', 'Deep'), _incoming('House', 'Tech'), scan_finished=False)

    assert report.held_back == ['House / Deep']
    assert report.created == ['House / Tech']
    assert navidrome.replaced == []
    assert await _outline(tree) == [('House', 'folder'), ('  Deep', 'playlist'), ('  Tech', 'playlist')]


@pytest.mark.anyio
async def test_a_playlist_navidrome_refused_gets_no_node(tree, navidrome, sessions):
    navidrome.refuse_create = True

    report = await _import(tree, _incoming('House', 'Deep'))

    assert report.failed == ['House / Deep']
    assert _nodes(sessions) == []


@pytest.mark.anyio
async def test_a_tree_read_during_the_create_waits_for_the_node(tree, navidrome, sessions):
    """Without the lock held across the create and the node write, the read would
    adopt the new playlist at the root, and the import's own node write would then
    break the unique (user, playlist)."""
    reads = []
    navidrome.on_create = lambda _: reads.append(asyncio.ensure_future(tree.get_tree(USER)))

    report = await _import(tree, _incoming('House', 'Deep'))
    body = await reads[0]

    assert report.created == ['House / Deep']
    assert [(n['name'], n['kind']) for n in body['nodes']] == [('House', 'folder'), ('Deep', 'playlist')]
    assert len(_nodes(sessions)) == 2


# --- Serato ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_a_serato_parent_crate_with_its_own_tracks_is_a_playlist_with_children(tree, navidrome, sessions):
    # The crate orchestrator's order: a crate, then its sub-crates. `Sets` has no
    # tracks of its own (or all were skipped), so it produced no playlist.
    await _import(
        tree,
        _incoming('Sets', 'House'),
        _incoming('Sets', 'House', 'Deep'),
        _incoming('Sets', 'Techno', 'Peak'),
        origin='serato',
    )

    assert await _outline(tree) == [
        ('Sets', 'folder'),
        ('  House', 'playlist'),
        ('    Deep', 'playlist'),
        ('  Techno', 'folder'),
        ('    Peak', 'playlist'),
    ]
    assert {n.origin for n in _nodes(sessions)} == {'serato'}


@pytest.mark.anyio
async def test_a_serato_crate_matches_the_same_path_imported_from_rekordbox(tree, navidrome, sessions):
    await _import(tree, _incoming('House', 'Deep'), origin='rekordbox')

    report = await _import(tree, _incoming('House', 'Deep', songs=('s9',)), origin='serato')

    assert report.updated == ['House / Deep']
    assert len(navidrome.playlists) == 1


# --- the Rekordbox controller -----------------------------------------------------------

@pytest.mark.anyio
@pytest.mark.parametrize('state, writes_paths', [('live', False), ('none', True)])
async def test_playlist_path_table_is_written_for_none_users_only(state, writes_paths):
    controller = RekordboxXMLController.__new__(RekordboxXMLController)
    controller._db_controller = mock.Mock()
    controller._db_controller.playlist_tree_state.return_value = state
    controller._subsonic_orchestrator = mock.Mock(update_tracks_with_subid=mock.AsyncMock())
    controller._playlist_tree = mock.Mock(import_playlists=mock.AsyncMock(return_value='report'))

    result = await controller._create_playlists_from_xml(
        USER, rekordbox_xml=None, subbox_playlists=[_incoming('House', 'Deep')], scan_finished=False)

    assert result == 'report'
    controller._playlist_tree.import_playlists.assert_awaited_once_with(
        USER, mock.ANY, origin='rekordbox', scan_finished=False)
    assert controller._db_controller.save_playlist_paths.called is writes_paths
