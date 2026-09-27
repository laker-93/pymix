"""
The playlist tree (#201): its invariants, and reconciliation against Navidrome on
every read. The database is real (SQLite); Navidrome is a list of playlists.
"""
import asyncio
from unittest import mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from pymix.controllers.db_controller import DbController
from pymix.controllers.playlist_tree_controller import (
    FOLDER, PLAYLIST, PlaylistTreeController, TreeInvariantError, TreeNotEnabled,
)
from pymix.model.db_tables import Base, PlaylistNodeRow, UserRow
from pymix.model.subboxplaylist import SubBoxPlaylist
from pymix.orchestrators.subsonic_orchestrator import SubsonicOrchestrator
from pymix.services import metrics
from pymix.services.tree_lock import TreeLocks

USER = {'username': 'dj', 'password': 'pw'}


class FakeNavidrome:
    """What owned_playlists answers: the user's own playlists, smart ones included."""

    def __init__(self):
        self.playlists: list[SubBoxPlaylist] = []
        self.calls = 0
        self.during_call = None

    def add(self, playlist_id, name, owner='dj', readonly=False):
        self.playlists.append(SubBoxPlaylist(name=name, subsonic_id=playlist_id, owner=owner, readonly=readonly))

    def remove(self, playlist_id):
        self.playlists = [p for p in self.playlists if p.subsonic_id != playlist_id]

    async def owned_playlists(self, user):
        self.calls += 1
        if self.during_call:
            self.during_call()
        return [p for p in self.playlists if p.owner == user['username']]


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
    return DbController(session_factory=sessions, app_env='test', max_library_size=0,
                        serving_music_path_base=str(tmp_path), staging_path=f'{tmp_path}/s/{{user}}/')


@pytest.fixture
def navidrome():
    return FakeNavidrome()


@pytest.fixture
def locks():
    return TreeLocks()


@pytest.fixture
def tree(sessions, db_controller, navidrome, locks):
    db_controller.set_playlist_tree_state('dj', 'live')
    return PlaylistTreeController(sessions, db_controller, navidrome, locks)


def _rows(sessions):
    with sessions() as session:
        return session.query(PlaylistNodeRow).all()


def _shape(body):
    """(name, parent name, position) per node, in the order the tree returned them."""
    names = {n['node_id']: n['name'] for n in body['nodes']}
    return [(n['name'], names.get(n['parent_id']), n['position']) for n in body['nodes']]


# --- tree state ---------------------------------------------------------------------

@pytest.mark.anyio
async def test_a_user_without_a_tree_gets_nothing_and_no_nodes_are_written(sessions, db_controller, navidrome, locks):
    navidrome.add('pl-1', 'Deep')
    tree = PlaylistTreeController(sessions, db_controller, navidrome, locks)

    with pytest.raises(TreeNotEnabled):
        await tree.get_tree(USER)
    with pytest.raises(TreeNotEnabled):
        await tree.create_node(USER, FOLDER, name='House')

    assert _rows(sessions) == [] and navidrome.calls == 0
    assert db_controller.playlist_tree_state('dj') == 'none'


# --- reconciliation -----------------------------------------------------------------

@pytest.mark.anyio
async def test_the_first_read_adopts_every_playlist_the_user_owns_at_the_root(tree, navidrome):
    navidrome.add('pl-1', 'Deep')
    navidrome.add('pl-2', 'Smart', readonly=True)
    navidrome.add('theirs', 'Public', owner='someone')

    body = await tree.get_tree(USER)

    assert _shape(body) == [('Deep', None, 0), ('Smart', None, 1)]
    assert [n['navidrome_playlist_id'] for n in body['nodes']] == ['pl-1', 'pl-2']
    assert all(n['kind'] == PLAYLIST and n['parent_id'] is None and n['child_count'] == 0 for n in body['nodes'])
    assert body['hidden_playlist_ids'] == []


@pytest.mark.anyio
async def test_a_read_is_idempotent_and_picks_up_a_new_playlist_and_a_rename(tree, navidrome, sessions):
    navidrome.add('pl-1', 'Deep')
    await tree.get_tree(USER)
    navidrome.add('pl-2', 'New')
    navidrome.playlists[0].name = 'Deeper'

    body = await tree.get_tree(USER)
    again = await tree.get_tree(USER)

    assert _shape(body) == _shape(again) == [('Deeper', None, 0), ('New', None, 1)]
    assert len(_rows(sessions)) == 2
    # The name was never stored.
    assert all(r.name is None for r in _rows(sessions))


@pytest.mark.anyio
async def test_a_playlist_deleted_in_navidrome_is_dropped_and_counted(tree, navidrome):
    for pid in ('pl-1', 'pl-2', 'pl-3'):
        navidrome.add(pid, pid)
    await tree.get_tree(USER)
    navidrome.remove('pl-2')
    before = metrics.playlist_nodes_orphaned_total._value.get()

    body = await tree.get_tree(USER)

    assert _shape(body) == [('pl-1', None, 0), ('pl-3', None, 1)]
    assert metrics.playlist_nodes_orphaned_total._value.get() == before + 1


@pytest.mark.anyio
async def test_a_dropped_playlists_children_move_up_into_its_place_in_order(tree, navidrome):
    # A Serato crate with its own tracks and sub-crates: a playlist with children.
    for pid in ('first', 'crate', 'a', 'b', 'last'):
        navidrome.add(pid, pid)
    body = await tree.get_tree(USER)
    ids = {n['name']: n['node_id'] for n in body['nodes']}
    await tree.move_node(USER, ids['a'], ids['crate'])
    await tree.move_node(USER, ids['b'], ids['crate'])
    assert _shape(await tree.get_tree(USER)) == [
        ('first', None, 0), ('crate', None, 1), ('a', 'crate', 0), ('b', 'crate', 1), ('last', None, 2)]
    navidrome.remove('crate')

    body = await tree.get_tree(USER)

    assert _shape(body) == [('first', None, 0), ('a', None, 1), ('b', None, 2), ('last', None, 3)]


@pytest.mark.anyio
async def test_a_hidden_playlist_is_left_out_of_the_nodes_and_listed_as_hidden(tree, navidrome, sessions, db_controller):
    navidrome.add('pl-1', 'Kept')
    navidrome.add('pl-2', 'Hidden')
    await tree.get_tree(USER)
    batch_id = db_controller.create_trash_batch('dj', 'nodes', 'Playlist Hidden', 3600, [])
    with sessions() as session:
        session.query(PlaylistNodeRow).filter_by(navidrome_playlist_id='pl-2').update({'trash_batch_id': batch_id})
        session.commit()

    body = await tree.get_tree(USER)

    assert _shape(body) == [('Kept', None, 0)]
    assert body['hidden_playlist_ids'] == ['pl-2']
    assert db_controller.hidden_playlist_ids('dj') == {'pl-2'}


@pytest.mark.anyio
async def test_a_hidden_playlist_deleted_outside_pymix_is_lost_and_the_rest_of_its_batch_kept(
        tree, navidrome, sessions, db_controller):
    navidrome.add('pl-1', 'One')
    navidrome.add('pl-2', 'Two')
    await tree.get_tree(USER)
    with sessions() as session:
        nodes = {r.navidrome_playlist_id: r.node_id for r in session.query(PlaylistNodeRow).all()}
    batch_id = db_controller.create_trash_batch('dj', 'nodes', '2 playlists', 3600, [
        {'state': 'restorable', 'snapshot': {'node_id': nodes['pl-1']}},
        {'state': 'restorable', 'snapshot': {'node_id': nodes['pl-2']}},
    ])
    with sessions() as session:
        session.query(PlaylistNodeRow).update({'trash_batch_id': batch_id})
        session.commit()
    navidrome.remove('pl-1')

    body = await tree.get_tree(USER)

    assert body['hidden_playlist_ids'] == ['pl-2']
    assert [r.navidrome_playlist_id for r in _rows(sessions)] == ['pl-2']
    items = db_controller.get_trash_batch(batch_id)['items']
    assert [(i['snapshot']['node_id'], i['state']) for i in items] == [(nodes['pl-1'], 'lost'), (nodes['pl-2'], 'restorable')]


@pytest.mark.anyio
async def test_reconciliation_holds_the_tree_lock(tree, navidrome, locks):
    navidrome.add('pl-1', 'Deep')
    seen = []
    navidrome.during_call = lambda: seen.append(locks.hold('dj').locked())

    await tree.get_tree(USER)

    assert seen == [True]


@pytest.mark.anyio
async def test_a_write_waits_for_a_read_in_progress(tree, navidrome, locks):
    order = []
    release = asyncio.Event()

    async def slow_owned(user):
        order.append('read started')
        await release.wait()
        order.append('read done')
        return []
    navidrome.owned_playlists = slow_owned

    read = asyncio.create_task(tree.get_tree(USER))
    await asyncio.sleep(0)
    write = asyncio.create_task(tree.create_node(USER, FOLDER, name='House'))
    await asyncio.sleep(0.01)
    order.append('released')
    release.set()
    await read
    await write
    order.append('written')

    assert order == ['read started', 'released', 'read done', 'written']


# --- invariants ---------------------------------------------------------------------

@pytest.mark.anyio
@pytest.mark.parametrize('kwargs, message', [
    ({'kind': FOLDER, 'name': 'X', 'navidrome_playlist_id': 'pl'}, 'a folder has no Navidrome playlist'),
    ({'kind': FOLDER}, 'a folder needs a name'),
    ({'kind': PLAYLIST}, 'needs its Navidrome playlist id'),
    ({'kind': PLAYLIST, 'navidrome_playlist_id': 'pl', 'name': 'X'}, "name lives in Navidrome"),
    ({'kind': 'crate', 'name': 'X'}, 'unknown node kind'),
    ({'kind': FOLDER, 'name': 'X', 'origin': 'itunes'}, 'unknown origin'),
    ({'kind': FOLDER, 'name': 'X', 'parent_id': 'no-such-node'}, 'no live node'),
])
async def test_a_node_that_would_break_an_invariant_is_refused(tree, sessions, kwargs, message):
    kind = kwargs.pop('kind')
    with pytest.raises(TreeInvariantError, match=message):
        await tree.create_node(USER, kind, **kwargs)
    assert _rows(sessions) == []


@pytest.mark.anyio
async def test_the_same_navidrome_playlist_cannot_have_two_nodes(tree):
    await tree.create_node(USER, PLAYLIST, navidrome_playlist_id='pl-1')
    with pytest.raises(Exception):
        await tree.create_node(USER, PLAYLIST, navidrome_playlist_id='pl-1')


@pytest.mark.anyio
async def test_a_node_inserted_mid_list_shifts_its_later_siblings(tree, navidrome):
    house = await tree.create_node(USER, FOLDER, name='House', source_path=['House'], origin='rekordbox')
    await tree.create_node(USER, FOLDER, name='A', parent_id=house)
    await tree.create_node(USER, FOLDER, name='C', parent_id=house)
    await tree.create_node(USER, FOLDER, name='B', parent_id=house, position=1)
    await tree.create_node(USER, FOLDER, name='Z', parent_id=house, position=99)

    body = await tree.get_tree(USER)

    assert _shape(body) == [('House', None, 0), ('A', 'House', 0), ('B', 'House', 1), ('C', 'House', 2), ('Z', 'House', 3)]
    assert body['nodes'][0]['child_count'] == 4


@pytest.mark.anyio
async def test_a_playlist_may_have_children(tree, navidrome):
    navidrome.add('pl-1', 'Crate')
    navidrome.add('pl-2', 'Sub-crate')
    crate = await tree.create_node(USER, PLAYLIST, navidrome_playlist_id='pl-1', source_path=['Crate'], origin='serato')
    await tree.create_node(USER, PLAYLIST, navidrome_playlist_id='pl-2', parent_id=crate)

    body = await tree.get_tree(USER)

    assert _shape(body) == [('Crate', None, 0), ('Sub-crate', 'Crate', 0)]


@pytest.mark.anyio
async def test_a_move_keeps_both_sibling_lists_dense(tree):
    a = await tree.create_node(USER, FOLDER, name='A')
    b = await tree.create_node(USER, FOLDER, name='B')
    x = await tree.create_node(USER, FOLDER, name='x', parent_id=a)
    await tree.create_node(USER, FOLDER, name='y', parent_id=a)
    await tree.create_node(USER, FOLDER, name='z', parent_id=b)

    await tree.move_node(USER, x, b, position=0)

    assert _shape(await tree.get_tree(USER)) == [
        ('A', None, 0), ('y', 'A', 0), ('B', None, 1), ('x', 'B', 0), ('z', 'B', 1)]


@pytest.mark.anyio
async def test_a_move_within_its_own_list_reorders_it(tree):
    ids = [await tree.create_node(USER, FOLDER, name=n) for n in 'abcd']

    await tree.move_node(USER, ids[0], None, position=2)
    await tree.move_node(USER, ids[3], None, position=0)

    assert [n for n, _, _ in _shape(await tree.get_tree(USER))] == ['d', 'b', 'c', 'a']
    assert [p for _, _, p in _shape(await tree.get_tree(USER))] == [0, 1, 2, 3]


@pytest.mark.anyio
async def test_a_node_cannot_move_into_itself_or_its_own_subtree(tree):
    top = await tree.create_node(USER, FOLDER, name='Top')
    mid = await tree.create_node(USER, FOLDER, name='Mid', parent_id=top)
    low = await tree.create_node(USER, FOLDER, name='Low', parent_id=mid)
    before = _shape(await tree.get_tree(USER))

    for target in (top, mid, low):
        with pytest.raises(TreeInvariantError, match='own subtree'):
            await tree.move_node(USER, top, target)

    assert _shape(await tree.get_tree(USER)) == before


# --- pymix's own listings (§4.2) ------------------------------------------------------

@pytest.mark.anyio
async def test_every_pymix_listing_leaves_out_other_users_and_hidden_playlists(tree, navidrome, sessions, db_controller):
    navidrome.add('pl-1', 'Mine')
    navidrome.add('pl-2', 'Hidden')
    await tree.get_tree(USER)
    batch_id = db_controller.create_trash_batch('dj', 'nodes', 'x', 3600, [])
    with sessions() as session:
        session.query(PlaylistNodeRow).filter_by(navidrome_playlist_id='pl-2').update({'trash_batch_id': batch_id})
        session.commit()
    client = mock.Mock()
    client.get_playlists = mock.AsyncMock(return_value=navidrome.playlists + [
        SubBoxPlaylist(name='Public', subsonic_id='theirs', owner='someone')])
    client.get_playlist_tracks = mock.AsyncMock(return_value=[])

    listed = await SubsonicOrchestrator(client, db_controller=db_controller).get_subsonic_playlists(USER)

    assert [p.subsonic_id for p in listed] == ['pl-1']
