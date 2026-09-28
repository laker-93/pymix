"""
#206: the tree's structural verbs. Create a folder, create a playlist inside one, and
rename, move and reorder a node, all of which the next tree read and the next
Rekordbox and Serato export show (design-playlists-and-undo §10).

Real database (SQLite), the real tree controller and Subsonic orchestrator, and the
fake Navidrome the import and export tests use.
"""
import asyncio

import pytest

from pymix.controllers.playlist_tree_controller import (
    UNCHANGED, NodeNotFound, PlaylistNotCreated, TreeInvariantError, TreeNotEnabled,
)
from pymix.model.db_tables import PlaylistNodeRow
from pymix.tests.fixtures.playlist_tree import (  # noqa: F401 (fixtures)
    USER, _import, _incoming, _nodes, _outline, _xml, add_batch, db_controller, navidrome, rekordbox, serato, sessions,
    set_tree_state, tree,
)


# --- create a folder -------------------------------------------------------------------

@pytest.mark.anyio
async def test_a_folder_is_made_where_asked_and_its_siblings_shift(tree):
    house = await tree.create_folder(USER, 'House')
    await tree.create_folder(USER, 'Techno')
    await tree.create_folder(USER, '  Ideas  ', position=1)
    deep = await tree.create_folder(USER, 'Deep', parent_id=house['node_id'])

    assert await _outline(tree) == [
        ('House', 'folder'), ('  Deep', 'folder'), ('Ideas', 'folder'), ('Techno', 'folder')]
    assert deep == {'node_id': deep['node_id'], 'parent_id': house['node_id'], 'position': 0, 'kind': 'folder',
                    'name': 'Deep', 'navidrome_playlist_id': None}


@pytest.mark.anyio
async def test_a_folder_made_in_subbox_has_no_source_path_so_an_import_never_takes_it_over(tree, sessions):
    await tree.create_folder(USER, 'House')

    await _import(tree, _incoming('House', 'Deep', songs=('1',)))

    # Two House folders: the user's, left empty, and the import's.
    assert await _outline(tree) == [('House', 'folder'), ('House', 'folder'), ('  Deep', 'playlist')]
    with sessions() as session:
        mine = session.query(PlaylistNodeRow).filter(PlaylistNodeRow.origin == 'subbox').one()
        assert mine.source_path is None and mine.name == 'House'


@pytest.mark.anyio
@pytest.mark.parametrize('name', ['', '   '])
async def test_a_blank_folder_name_is_refused(tree, sessions, name):
    with pytest.raises(TreeInvariantError, match='needs a name'):
        await tree.create_folder(USER, name)
    assert _nodes(sessions) == []


@pytest.mark.anyio
async def test_a_folder_under_a_node_that_isnt_the_users_is_not_found(tree, sessions):
    with pytest.raises(NodeNotFound):
        await tree.create_folder(USER, 'Deep', parent_id='no-such-node')
    assert _nodes(sessions) == []


# --- create a playlist inside a folder -------------------------------------------------

@pytest.mark.anyio
async def test_a_playlist_is_created_in_navidrome_and_placed_in_its_folder_in_one_call(tree, navidrome, sessions):
    house = await tree.create_folder(USER, 'House')
    await tree.create_folder(USER, 'Deep', parent_id=house['node_id'])

    node = await tree.create_playlist(USER, 'Sunday', parent_id=house['node_id'], song_ids=['3', '1', '2'])

    [(playlist_id, playlist)] = navidrome.playlists.items()
    assert playlist.name == 'Sunday' and navidrome.entries[playlist_id] == ['3', '1', '2']
    assert node == {'node_id': node['node_id'], 'parent_id': house['node_id'], 'position': 1, 'kind': 'playlist',
                    'name': 'Sunday', 'navidrome_playlist_id': playlist_id}
    with sessions() as session:
        row = session.get(PlaylistNodeRow, node['node_id'])
        assert (row.origin, row.source_path, row.name) == ('subbox', None, None)
    # Where it was put, not adopted at the root.
    assert await _outline(tree) == [('House', 'folder'), ('  Deep', 'folder'), ('  Sunday', 'playlist')]


@pytest.mark.anyio
async def test_an_empty_playlist_can_be_made_at_the_root(tree, navidrome):
    node = await tree.create_playlist(USER, 'Ideas')

    assert node['parent_id'] is None and navidrome.entries[node['navidrome_playlist_id']] == []


@pytest.mark.anyio
async def test_a_bad_parent_creates_nothing_in_navidrome(tree, navidrome, sessions):
    with pytest.raises(NodeNotFound):
        await tree.create_playlist(USER, 'Sunday', parent_id='no-such-node', song_ids=['1'])
    assert navidrome.playlists == {} and _nodes(sessions) == []


@pytest.mark.anyio
async def test_a_playlist_navidrome_refuses_leaves_no_node(tree, navidrome, sessions):
    navidrome.refuse_create = True
    with pytest.raises(PlaylistNotCreated):
        await tree.create_playlist(USER, 'Sunday')
    assert _nodes(sessions) == []


@pytest.mark.anyio
async def test_a_blank_playlist_name_is_refused_before_navidrome_is_asked(tree, navidrome):
    with pytest.raises(TreeInvariantError, match='needs a name'):
        await tree.create_playlist(USER, '  ')
    assert navidrome.playlists == {}


@pytest.mark.anyio
async def test_a_tree_read_during_the_create_cannot_adopt_the_playlist_at_the_root(tree, navidrome):
    house = await tree.create_folder(USER, 'House')
    reads = []
    # Navidrome has the playlist before its node is written: a read now would adopt it.
    navidrome.on_create = lambda playlist_id: reads.append(asyncio.ensure_future(tree.get_tree(USER)))

    await tree.create_playlist(USER, 'Sunday', parent_id=house['node_id'])
    await reads[0]

    assert await _outline(tree) == [('House', 'folder'), ('  Sunday', 'playlist')]


# --- rename, move, reorder -----------------------------------------------------------

async def _abc(tree, parent_id=None):
    return [(await tree.create_folder(USER, n, parent_id=parent_id))['node_id'] for n in 'abc']


@pytest.mark.anyio
async def test_a_folder_is_renamed_where_it_is(tree):
    a, b, c = await _abc(tree)

    node = await tree.update_node(USER, b, name=' Bee ')

    assert node['name'] == 'Bee' and node['position'] == 1
    assert await _outline(tree) == [('a', 'folder'), ('Bee', 'folder'), ('c', 'folder')]


@pytest.mark.anyio
async def test_a_playlist_is_not_renamed_in_the_tree(tree, navidrome):
    node = await tree.create_playlist(USER, 'Sunday')

    with pytest.raises(TreeInvariantError, match='renamed in Navidrome'):
        await tree.update_node(USER, node['node_id'], name='Monday')
    assert [p.name for p in navidrome.playlists.values()] == ['Sunday']


@pytest.mark.anyio
async def test_a_blank_rename_is_refused_and_changes_nothing(tree):
    a, b, c = await _abc(tree)

    with pytest.raises(TreeInvariantError, match='needs a name'):
        await tree.update_node(USER, b, name='')
    assert [n for n, _ in await _outline(tree)] == ['a', 'b', 'c']


@pytest.mark.anyio
async def test_a_position_alone_reorders_under_the_same_parent(tree):
    top = (await tree.create_folder(USER, 'top'))['node_id']
    a, b, c = await _abc(tree, top)

    await tree.update_node(USER, c, position=0)
    await tree.update_node(USER, c, position=99)   # clamped to the end

    assert await _outline(tree) == [('top', 'folder'), ('  a', 'folder'), ('  b', 'folder'), ('  c', 'folder')]
    await tree.update_node(USER, a, position=1)
    assert [n.strip() for n, _ in await _outline(tree)] == ['top', 'b', 'a', 'c']


@pytest.mark.anyio
async def test_a_move_to_the_root_is_parent_none_and_leaves_both_lists_dense(tree, sessions):
    top = (await tree.create_folder(USER, 'top'))['node_id']
    a, b, c = await _abc(tree, top)

    await tree.update_node(USER, b, parent_id=None, position=0)

    assert await _outline(tree) == [('b', 'folder'), ('top', 'folder'), ('  a', 'folder'), ('  c', 'folder')]
    with sessions() as session:
        positions = {r.name: r.position for r in session.query(PlaylistNodeRow).all()}
    assert positions == {'b': 0, 'top': 1, 'a': 0, 'c': 1}


@pytest.mark.anyio
async def test_a_move_without_a_position_goes_to_the_end_and_takes_its_subtree(tree, navidrome):
    house, techno = [(await tree.create_folder(USER, n))['node_id'] for n in ('House', 'Techno')]
    await tree.create_folder(USER, 'Peak', parent_id=techno)
    deep = (await tree.create_folder(USER, 'Deep', parent_id=house))['node_id']
    await tree.create_playlist(USER, 'Sunday', parent_id=deep)

    await tree.update_node(USER, deep, parent_id=techno)

    assert await _outline(tree) == [
        ('House', 'folder'), ('Techno', 'folder'), ('  Peak', 'folder'), ('  Deep', 'folder'),
        ('    Sunday', 'playlist')]


@pytest.mark.anyio
async def test_moving_under_the_parent_it_already_has_with_no_position_changes_nothing(tree):
    a, b, c = await _abc(tree)

    await tree.update_node(USER, a, parent_id=None)

    assert [n for n, _ in await _outline(tree)] == ['a', 'b', 'c']


@pytest.mark.anyio
async def test_a_rename_and_a_move_in_one_call(tree):
    a, b, c = await _abc(tree)

    await tree.update_node(USER, c, name='see', parent_id=a, position=0)

    assert await _outline(tree) == [('a', 'folder'), ('  see', 'folder'), ('b', 'folder')]


@pytest.mark.anyio
async def test_a_refused_move_changes_nothing_not_even_a_rename_in_the_same_call(tree):
    top = (await tree.create_folder(USER, 'top'))['node_id']
    mid = (await tree.create_folder(USER, 'mid', parent_id=top))['node_id']

    with pytest.raises(TreeInvariantError, match='own subtree'):
        await tree.update_node(USER, top, name='renamed', parent_id=mid)
    assert await _outline(tree) == [('top', 'folder'), ('  mid', 'folder')]


@pytest.mark.anyio
async def test_a_node_moved_under_itself_is_refused(tree):
    [a, *_] = await _abc(tree)
    with pytest.raises(TreeInvariantError, match='own subtree'):
        await tree.update_node(USER, a, parent_id=a)


@pytest.mark.anyio
@pytest.mark.parametrize('target', ['node', 'parent'])
async def test_a_trashed_or_unknown_node_is_not_found(tree, sessions, target):
    a, b, c = await _abc(tree)
    with sessions() as session:
        add_batch(session)
        session.get(PlaylistNodeRow, b).trash_batch_id = 'batch-1'
        session.commit()

    for missing in (b, 'no-such-node'):
        with pytest.raises(NodeNotFound):
            if target == 'node':
                await tree.update_node(USER, missing, position=0)
            else:
                await tree.update_node(USER, a, parent_id=missing)


@pytest.mark.anyio
async def test_another_users_node_is_not_found(tree, sessions):
    [a, *_] = await _abc(tree)
    with sessions() as session:
        session.get(PlaylistNodeRow, a).user_id = 'someone-else'
        session.commit()

    with pytest.raises(NodeNotFound):
        await tree.update_node(USER, a, name='mine now')


# --- tree state ----------------------------------------------------------------------

@pytest.mark.anyio
async def test_a_user_without_a_tree_can_do_none_of_it(tree, navidrome, sessions):
    [a, *_] = await _abc(tree)
    set_tree_state(sessions, 'none')

    for write in (tree.create_folder(USER, 'x'), tree.create_playlist(USER, 'x'),
                  tree.update_node(USER, a, name='x')):
        with pytest.raises(TreeNotEnabled):
            await write
    assert navidrome.playlists == {}


# --- #206's "done when" ------------------------------------------------------------------

@pytest.mark.anyio
async def test_a_folder_created_renamed_moved_and_reordered_shows_in_the_tree_and_both_exports(
        tree, rekordbox, serato, navidrome):
    await _import(tree, _incoming('House', 'Deep', songs=('1',)), _incoming('Loose', songs=('2',)))
    body = await tree.get_tree(USER)
    by_name = {n['name']: n['node_id'] for n in body['nodes']}

    sets = await tree.create_folder(USER, 'Sets')
    await tree.create_playlist(USER, 'Sunday', parent_id=sets['node_id'], song_ids=['3'])
    await tree.update_node(USER, sets['node_id'], name='Live Sets')                  # rename
    await tree.update_node(USER, sets['node_id'], parent_id=by_name['House'])        # move
    await tree.update_node(USER, sets['node_id'], position=0)                        # reorder
    await tree.update_node(USER, by_name['Loose'], position=0)

    assert await _outline(tree) == [
        ('Loose', 'playlist'), ('House', 'folder'), ('  Live Sets', 'folder'), ('    Sunday', 'playlist'),
        ('  Deep', 'playlist')]
    assert await _xml(rekordbox) == [
        ('Loose', 'playlist', [2]),
        ('House', 'folder', None),
        ('  Live Sets', 'folder', None),
        ('    Sunday', 'playlist', [3]),
        ('  Deep', 'playlist', [1]),
        ('NOPLAYLIST', 'playlist', []),
    ]
    crates = (await serato.get_export_structure(USER)).crates
    assert [c.path_components for c in crates] == [['Loose'], ['House', 'Live Sets', 'Sunday'], ['House', 'Deep']]


def test_unchanged_is_not_none():
    # None is the root; the two must never be confused.
    assert UNCHANGED is not None and bool(UNCHANGED)
