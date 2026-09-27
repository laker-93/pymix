"""
#207: deleting a playlist or folder hides it, restore is synchronous, and the purge
is the one place a playlist is deleted from Navidrome (design-playlists-and-undo
§8.2, §9, §13). A real database and tree controller, over the fake Navidrome.
"""
import pytest

from pymix.controllers.playlist_tree_controller import (
    NodeNotFound, NothingToRestore, TreeInvariantError, TreeNotEnabled,
)
from pymix.model.db_tables import PlaylistNodeRow, TrashBatchRow, TrashItemRow
from pymix.services.trash import ItemState, batch_state
from pymix.tests.fixtures.playlist_tree import (  # noqa: F401 (fixtures)
    USER, _import, _incoming, _nodes, _outline, _xml, db_controller, navidrome, rekordbox, sessions, tree,
)


def _shape(sessions):
    """Every node as it is stored: what an identical tree must match exactly."""
    return sorted((n.node_id, n.parent_id, n.position, n.kind, n.name, n.navidrome_playlist_id, n.trash_batch_id)
                  for n in _nodes(sessions))


async def _ids(tree):
    """{name: node_id} of the live tree."""
    return {n['name']: n['node_id'] for n in (await tree.get_tree(USER))['nodes']}


def _items(sessions, batch_id):
    with sessions() as session:
        return session.query(TrashItemRow).filter(TrashItemRow.batch_id == batch_id).order_by(TrashItemRow.id).all()


def _pid(navidrome, name):
    [playlist_id] = [pid for pid, p in navidrome.playlists.items() if p.name == name]
    return playlist_id


async def _house(tree):
    """A folder with 2 sub-folders and 6 playlists, and a sibling either side."""
    await _import(tree, _incoming('Before', songs=('0',)))
    await _import(tree, *(_incoming('House', *path, songs=(str(i), str(i + 10))) for i, path in enumerate([
        ('2024', 'Deep'), ('2024', 'Tech'), ('2025', 'Peak'), ('2025', 'Warmup'), ('Loose',), ('Ideas',)], 1)))
    await _import(tree, _incoming('After', songs=('9',)))


# --- #207's "done when" -----------------------------------------------------------------

@pytest.mark.anyio
async def test_a_folder_deleted_and_restored_is_identical_and_hidden_in_between(
        tree, sessions, navidrome, rekordbox):
    await _house(tree)
    before, outline = _shape(sessions), await _outline(tree)
    entries = {pid: list(e) for pid, e in navidrome.entries.items()}
    house = (await _ids(tree))['House']

    deleted = await tree.delete_nodes(USER, [house])

    assert deleted['label'] == 'Folder House · 6 playlists'
    assert (deleted['deleted']['folders'], deleted['deleted']['playlists']) == (3, 6)
    body = await tree.get_tree(USER)
    assert [n['name'] for n in body['nodes']] == ['Before', 'After']
    assert [n['position'] for n in body['nodes']] == [0, 1]
    hidden = {_pid(navidrome, n) for n in ('Deep', 'Tech', 'Peak', 'Warmup', 'Loose', 'Ideas')}
    assert set(body['hidden_playlist_ids']) == hidden
    # Absent from every list pymix gives: its own listing and both exports.
    assert {p.subsonic_id for p in await tree._subsonic.get_subsonic_playlists(USER)} & hidden == set()
    assert [name for name, *_ in await _xml(rekordbox)] == ['Before', 'After', 'NOPLAYLIST']
    # Nothing was deleted, renamed or rewritten in Navidrome.
    assert len(navidrome.playlists) == 8 and navidrome.deleted == [] and navidrome.renamed == []
    assert navidrome.entries == entries

    restored = await tree.restore_nodes(USER, deleted['trash_batch_id'])

    assert restored['success'] and restored['moved'] == [] and restored['shrunk'] == []
    assert len(restored['restored']) == 9
    after = _shape(sessions)
    assert [n[:6] for n in after] == [n[:6] for n in before]
    assert all(n[6] is None for n in after)
    assert await _outline(tree) == outline
    assert navidrome.entries == entries and (await tree.get_tree(USER))['hidden_playlist_ids'] == []
    assert batch_state(i.state for i in _items(sessions, deleted['trash_batch_id'])) == 'restored'


# --- delete --------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_a_delete_is_one_batch_with_an_item_per_node_and_counts_the_entries(tree, sessions, navidrome):
    await _import(tree, _incoming('House', 'Deep', songs=('1', '2', '3')))
    navidrome.add('Smart', readonly=True, songs=('1',))
    ids = await _ids(tree)

    deleted = await tree.delete_nodes(USER, [ids['House'], ids['Smart']])

    with sessions() as session:
        [batch] = session.query(TrashBatchRow).all()
    assert (batch.batch_id, batch.kind, batch.label, batch.bytes) == (
        deleted['trash_batch_id'], 'nodes', '1 folder · 2 playlists', 0)
    snapshots = {i.snapshot['name']: i.snapshot for i in _items(sessions, batch.batch_id)}
    assert {n: s['kind'] for n, s in snapshots.items()} == {'House': 'folder', 'Deep': 'playlist', 'Smart': 'playlist'}
    assert snapshots['Deep']['n_entries'] == 3 and snapshots['Deep']['parent_name'] == 'House'
    # A smart playlist's tracks are its rules' to change: nothing to compare.
    assert snapshots['Smart']['smart'] is True and snapshots['Smart']['n_entries'] is None
    assert {i.state for i in _items(sessions, batch.batch_id)} == {ItemState.RESTORABLE.value}


@pytest.mark.anyio
async def test_a_node_and_its_own_descendant_are_deleted_once(tree, sessions):
    await _import(tree, _incoming('House', 'Deep', songs=('1',)))
    ids = await _ids(tree)

    deleted = await tree.delete_nodes(USER, [ids['Deep'], ids['House'], ids['Deep']])

    assert deleted['deleted']['node_ids'] == [ids['House'], ids['Deep']]
    assert deleted['label'] == 'Folder House · 1 playlist'
    assert len(_items(sessions, deleted['trash_batch_id'])) == 2


@pytest.mark.anyio
async def test_a_playlist_label(tree):
    await _import(tree, _incoming('Deep', songs=('1',)))

    assert (await tree.delete_nodes(USER, [(await _ids(tree))['Deep']]))['label'] == 'Playlist Deep'


@pytest.mark.anyio
@pytest.mark.parametrize('bad', ['missing', 'trashed', 'theirs'])
async def test_a_delete_with_one_bad_id_deletes_nothing(tree, sessions, bad):
    await _import(tree, _incoming('A', songs=('1',)), _incoming('B', songs=('2',)))
    ids = await _ids(tree)
    target = {'missing': 'no-such-node', 'trashed': ids['B'], 'theirs': 'their-node'}[bad]
    if bad == 'trashed':
        await tree.delete_nodes(USER, [ids['B']])
    if bad == 'theirs':
        with sessions() as session:
            session.add(PlaylistNodeRow(node_id='their-node', user_id='user-2', position=0, kind='folder',
                                        name='X', origin='subbox', created_at=0, updated_at=0))
            session.commit()
    before = _shape(sessions)

    with pytest.raises(NodeNotFound):
        await tree.delete_nodes(USER, [ids['A'], target])

    assert _shape(sessions) == before


@pytest.mark.anyio
async def test_nothing_to_delete_is_refused(tree):
    with pytest.raises(TreeInvariantError):
        await tree.delete_nodes(USER, [])


@pytest.mark.anyio
async def test_nothing_can_be_moved_into_a_trashed_folder(tree):
    await _import(tree, _incoming('House', 'Deep', songs=('1',)), _incoming('Loose', songs=('2',)))
    ids = await _ids(tree)
    await tree.delete_nodes(USER, [ids['House']])

    with pytest.raises(NodeNotFound):
        await tree.move_node(USER, ids['Loose'], ids['House'])


@pytest.mark.anyio
async def test_a_user_with_a_playlist_in_the_trash_cannot_be_rolled_back(tree, db_controller):
    await _import(tree, _incoming('Deep', songs=('1',)))
    await tree.delete_nodes(USER, [(await _ids(tree))['Deep']])

    with pytest.raises(TreeInvariantError):
        await tree.rollback(USER)
    assert db_controller.playlist_tree_state('dj') == 'live'


# --- restore: where each node goes ----------------------------------------------------------

@pytest.mark.anyio
async def test_siblings_deleted_together_go_back_in_their_own_gaps(tree):
    await _import(tree, *(_incoming(n, songs=(str(i),)) for i, n in enumerate('ABCDE')))
    ids = await _ids(tree)
    deleted = await tree.delete_nodes(USER, [ids['D'], ids['A'], ids['C']])
    assert [n for n, _ in await _outline(tree)] == ['B', 'E']

    await tree.restore_nodes(USER, deleted['trash_batch_id'])

    assert [n for n, _ in await _outline(tree)] == list('ABCDE')


@pytest.mark.anyio
async def test_a_position_that_no_longer_exists_is_clamped(tree):
    await _import(tree, *(_incoming(n, songs=(str(i),)) for i, n in enumerate('ABC')))
    ids = await _ids(tree)
    deleted = await tree.delete_nodes(USER, [ids['C']])
    await tree.delete_nodes(USER, [ids['A'], ids['B']])
    await _import(tree, _incoming('D', songs=('4',)))

    restored = await tree.restore_nodes(USER, deleted['trash_batch_id'])

    assert [n for n, _ in await _outline(tree)] == ['D', 'C'] and restored['moved'] == []


@pytest.mark.anyio
async def test_restoring_twice_is_refused(tree):
    await _import(tree, _incoming('Deep', songs=('1',)))
    deleted = await tree.delete_nodes(USER, [(await _ids(tree))['Deep']])
    await tree.restore_nodes(USER, deleted['trash_batch_id'])

    with pytest.raises(NothingToRestore):
        await tree.restore_nodes(USER, deleted['trash_batch_id'])


@pytest.mark.anyio
async def test_a_batch_that_is_not_a_playlist_delete_is_not_found(tree, db_controller):
    batch_id = db_controller.create_trash_batch('dj', 'track', '1 track', 60, [{'state': 'restorable'}])

    with pytest.raises(NodeNotFound):
        await tree.restore_nodes(USER, batch_id)
    with pytest.raises(NodeNotFound):
        await tree.restore_nodes(USER, 'no-such-batch')


# --- §9, every row with a node in it -----------------------------------------------------

@pytest.mark.anyio
async def test_s9_a_smart_playlist_comes_back_with_its_rules_untouched(tree, navidrome):
    smart = navidrome.add('Smart', readonly=True, songs=('1', '2'))
    deleted = await tree.delete_nodes(USER, [(await _ids(tree))['Smart']])
    assert (await tree.get_tree(USER))['hidden_playlist_ids'] == [smart]
    # Its rules re-evaluate to something else meanwhile: not a loss, and not reported.
    navidrome.entries[smart] = ['1']

    restored = await tree.restore_nodes(USER, deleted['trash_batch_id'])

    assert [n['navidrome_playlist_id'] for n in restored['restored']] == [smart] and restored['shrunk'] == []
    assert navidrome.playlists[smart].readonly and navidrome.renamed == [] and navidrome.replaced == []


@pytest.mark.anyio
async def test_s9_delete_track_then_playlist_then_undo_the_playlist_reports_no_loss(tree, navidrome):
    # T in the trash: Navidrome's row is missing, and the entry is kept (#210).
    await _import(tree, _incoming('P', songs=('t', 'u')))
    navidrome.missing.add('t')
    deleted = await tree.delete_nodes(USER, [(await _ids(tree))['P']])

    restored = await tree.restore_nodes(USER, deleted['trash_batch_id'])

    assert restored['shrunk'] == []
    # ...and T's own undo puts it back in P: its entry was never removed.
    navidrome.missing.discard('t')
    assert [r['mediaFileId'] for r in await navidrome.playlist_tracks(USER, _pid(navidrome, 'P'))] == ['t', 'u']


@pytest.mark.anyio
async def test_s9_delete_playlist_then_track_then_undo_both(tree, navidrome):
    await _import(tree, _incoming('P', songs=('t', 'u')))
    deleted = await tree.delete_nodes(USER, [(await _ids(tree))['P']])
    navidrome.missing.add('t')      # delete T while P is hidden
    navidrome.missing.discard('t')  # undo T: back in P, although P is hidden

    restored = await tree.restore_nodes(USER, deleted['trash_batch_id'])

    assert restored['shrunk'] == []
    assert [r['mediaFileId'] for r in await navidrome.playlist_tracks(USER, _pid(navidrome, 'P'))] == ['t', 'u']


@pytest.mark.anyio
@pytest.mark.parametrize('n_purged, message', [
    (1, '1 track in P was permanently deleted while it was in the trash.'),
    (2, '2 tracks in P were permanently deleted while it was in the trash.'),
])
async def test_s9_a_track_purged_while_the_playlist_was_hidden_is_counted(tree, navidrome, n_purged, message):
    await _import(tree, _incoming('P', songs=('t', 'u', 'v')))
    deleted = await tree.delete_nodes(USER, [(await _ids(tree))['P']])
    navidrome.purged.update(['t', 'u'][:n_purged])
    # Navidrome doesn't refresh a stored songCount on a purge: the count is live.
    assert navidrome.playlists[_pid(navidrome, 'P')].n_of_songs == 3

    restored = await tree.restore_nodes(USER, deleted['trash_batch_id'])

    [shrunk] = restored['shrunk']
    assert (shrunk['name'], shrunk['n_tracks_lost'], shrunk['message']) == ('P', n_purged, message)
    assert restored['success']


@pytest.mark.anyio
async def test_s9_a_count_that_could_not_be_read_reports_nothing(tree, navidrome):
    await _import(tree, _incoming('P', songs=('t', 'u')))
    real = navidrome.playlist_tracks

    async def broken(user, playlist_id):
        raise ConnectionError('navidrome down')
    navidrome.playlist_tracks = broken
    deleted = await tree.delete_nodes(USER, [(await _ids(tree))['P']])
    navidrome.playlist_tracks = real
    navidrome.purged.add('t')

    restored = await tree.restore_nodes(USER, deleted['trash_batch_id'])

    assert len(restored['restored']) == 1 and restored['shrunk'] == []


@pytest.mark.anyio
async def test_s9_a_folder_comes_back_with_its_playlists_in_order_and_in_its_place(tree):
    await _import(tree, _incoming('A', songs=('0',)), _incoming('F', 'P1', songs=('1',)),
                  _incoming('F', 'P2', songs=('2',)), _incoming('Z', songs=('3',)))
    outline = await _outline(tree)
    deleted = await tree.delete_nodes(USER, [(await _ids(tree))['F']])

    await tree.restore_nodes(USER, deleted['trash_batch_id'])

    assert await _outline(tree) == outline


@pytest.mark.anyio
async def test_s9_delete_p1_then_its_folder_undo_the_folder_then_p1(tree):
    await _import(tree, _incoming('F', 'P1', songs=('1',)), _incoming('F', 'P2', songs=('2',)),
                  _incoming('F', 'P3', songs=('3',)))
    outline = await _outline(tree)
    ids = await _ids(tree)
    p1 = await tree.delete_nodes(USER, [ids['P1']])
    f = await tree.delete_nodes(USER, [ids['F']])
    assert f['label'] == 'Folder F · 2 playlists'

    await tree.restore_nodes(USER, f['trash_batch_id'])
    assert [n for n, _ in await _outline(tree)] == ['F', '  P2', '  P3']
    restored = await tree.restore_nodes(USER, p1['trash_batch_id'])

    assert restored['moved'] == [] and await _outline(tree) == outline


@pytest.mark.anyio
async def test_undoing_p1_while_its_folder_is_in_the_trash_moves_it_up_and_says_so(tree):
    await _import(tree, _incoming('Sets', 'House', 'P1', songs=('1',)), _incoming('Sets', 'House', 'P2', songs=('2',)),
                  _incoming('Sets', 'Other', songs=('3',)))
    ids = await _ids(tree)
    p1 = await tree.delete_nodes(USER, [ids['P1']])
    await tree.delete_nodes(USER, [ids['House']])

    restored = await tree.restore_nodes(USER, p1['trash_batch_id'])

    [moved] = restored['moved']
    assert (moved['node_id'], moved['parent_id'], moved['parent_name']) == (ids['P1'], ids['Sets'], 'Sets')
    assert moved['message'] == 'P1 restored to Sets: House was deleted separately.'
    # At the end of its new parent.
    assert [n for n, _ in await _outline(tree)] == ['Sets', '  Other', '  P1']


@pytest.mark.anyio
async def test_undoing_p1_after_its_folder_was_purged_moves_it_to_the_root(tree, db_controller, sessions, navidrome):
    await _import(tree, _incoming('F', 'P1', songs=('1',)), _incoming('F', 'P2', songs=('2',)),
                  _incoming('Loose', songs=('3',)))
    ids = await _ids(tree)
    p1 = await tree.delete_nodes(USER, [ids['P1']])
    f = await tree.delete_nodes(USER, [ids['F']])
    batch = db_controller.get_trash_batch(f['trash_batch_id'])
    assert await tree.purge_nodes(batch, batch['items']) == (2, [])

    restored = await tree.restore_nodes(USER, p1['trash_batch_id'])

    [moved] = restored['moved']
    assert (moved['parent_id'], moved['parent_name']) == (None, None)
    assert moved['message'] == 'P1 restored to the top level: F was deleted separately.'
    assert [n for n, _ in await _outline(tree)] == ['Loose', 'P1']
    assert set(navidrome.playlists) == {_pid(navidrome, 'P1'), _pid(navidrome, 'Loose')}


@pytest.mark.anyio
async def test_a_hidden_playlist_deleted_outside_pymix_is_lost_and_the_rest_comes_back(tree, navidrome, sessions):
    await _import(tree, _incoming('F', 'P1', songs=('1',)), _incoming('F', 'P2', songs=('2',)))
    deleted = await tree.delete_nodes(USER, [(await _ids(tree))['F']])
    # An old client deletes P1 directly in Navidrome.
    del navidrome.playlists[_pid(navidrome, 'P1')]

    restored = await tree.restore_nodes(USER, deleted['trash_batch_id'])

    assert not restored['success']
    assert [(n['name'], n['message']) for n in restored['lost']] == [
        ('P1', "P1 was deleted outside subbox while it was in the trash, and can't be restored.")]
    assert [n for n, _ in await _outline(tree)] == ['F', '  P2']
    states = {i.snapshot['name']: i.state for i in _items(sessions, deleted['trash_batch_id'])}
    assert states == {'F': 'restored', 'P1': 'lost', 'P2': 'restored'}


# --- purge ------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_the_purge_deletes_the_playlists_from_navidrome_and_the_nodes(tree, db_controller, sessions, navidrome):
    await _import(tree, _incoming('F', 'P1', songs=('1',)), _incoming('F', 'P2', songs=('2',)),
                  _incoming('Loose', songs=('3',)))
    ids = await _ids(tree)
    doomed = {_pid(navidrome, 'P1'), _pid(navidrome, 'P2')}
    deleted = await tree.delete_nodes(USER, [ids['F']])
    batch = db_controller.get_trash_batch(deleted['trash_batch_id'])

    assert await tree.purge_nodes(batch, batch['items']) == (3, [])

    assert set(navidrome.deleted) == doomed and set(navidrome.playlists) == {_pid(navidrome, 'Loose')}
    assert [n.node_id for n in _nodes(sessions)] == [ids['Loose']]
    assert batch_state(i.state for i in _items(sessions, deleted['trash_batch_id'])) == 'purged'


@pytest.mark.anyio
async def test_a_playlist_already_gone_from_navidrome_counts_as_purged(tree, db_controller, sessions, navidrome):
    await _import(tree, _incoming('P', songs=('1',)))
    deleted = await tree.delete_nodes(USER, [(await _ids(tree))['P']])
    navidrome.playlists.clear()
    batch = db_controller.get_trash_batch(deleted['trash_batch_id'])

    assert await tree.purge_nodes(batch, batch['items']) == (1, [])
    assert _nodes(sessions) == []


@pytest.mark.anyio
async def test_a_playlist_navidrome_would_not_delete_is_retried_on_the_next_purge(
        tree, db_controller, sessions, navidrome):
    await _import(tree, _incoming('F', 'P1', songs=('1',)), _incoming('F', 'P2', songs=('2',)))
    ids = await _ids(tree)
    p1 = _pid(navidrome, 'P1')
    navidrome.refuse_delete.add(p1)
    deleted = await tree.delete_nodes(USER, [ids['F']])
    batch = db_controller.get_trash_batch(deleted['trash_batch_id'])
    db_controller.update_trash_items(batch['batch_id'], {i['id']: {'state': 'expired'} for i in batch['items']})

    n, errors = await tree.purge_nodes(batch, batch['items'])

    assert n == 2 and len(errors) == 1 and p1 in errors[0]
    # F went (its node was only a folder); P1's node stays, hidden, pointing nowhere gone.
    [left] = _nodes(sessions)
    assert (left.node_id, left.parent_id, left.trash_batch_id) == (ids['P1'], None, batch['batch_id'])
    assert (await tree.get_tree(USER))['hidden_playlist_ids'] == [p1]
    assert {i.snapshot['name']: i.state for i in _items(sessions, batch['batch_id'])} == {
        'F': 'purged', 'P1': 'expired', 'P2': 'purged'}

    navidrome.refuse_delete.clear()
    batch = db_controller.get_trash_batch(deleted['trash_batch_id'])
    assert await tree.purge_nodes(batch, [i for i in batch['items'] if i['state'] == 'expired']) == (1, [])
    assert _nodes(sessions) == [] and navidrome.playlists == {}


@pytest.mark.anyio
async def test_a_purge_that_stopped_after_the_deletes_is_finished_by_reconciliation(
        tree, db_controller, sessions, navidrome):
    await _import(tree, _incoming('P', songs=('1',)), _incoming('Loose', songs=('2',)))
    deleted = await tree.delete_nodes(USER, [(await _ids(tree))['P']])
    batch_id = deleted['trash_batch_id']
    # The purge marked the item expired, deleted the playlist, and pymix died.
    db_controller.update_trash_items(batch_id, {i.id: {'state': 'expired'} for i in _items(sessions, batch_id)})
    del navidrome.playlists[_pid(navidrome, 'P')]

    await tree.get_tree(USER)

    # Not `lost`: nothing outside pymix deleted it.
    assert [i.state for i in _items(sessions, batch_id)] == ['purged']
    assert [n.navidrome_playlist_id for n in _nodes(sessions)] == [_pid(navidrome, 'Loose')]


# --- users without a tree ---------------------------------------------------------------------

@pytest.mark.anyio
async def test_a_user_without_a_tree_cannot_delete_or_restore(tree, db_controller, navidrome):
    navidrome.add('Deep')
    db_controller.set_playlist_tree_state('dj', 'none')

    with pytest.raises(TreeNotEnabled):
        await tree.delete_nodes(USER, ['any'])
    with pytest.raises(TreeNotEnabled):
        await tree.restore_nodes(USER, 'any')
    assert len(navidrome.playlists) == 1
