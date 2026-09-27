"""
#202: a Rekordbox or Serato import builds the user's playlist tree, and a
re-import matches by `source_path`, so a playlist the user moved or renamed in subbox
is updated in place rather than duplicated (design-playlists-and-undo §5.1, §5.2).

The database is real (SQLite), and so are the tree controller and the Subsonic
orchestrator; Navidrome is a fake that keeps playlists the way it does.
"""
import asyncio
import logging
from unittest import mock

import pytest

from pymix.controllers.playlist_tree_controller import TreeNotEnabled
from pymix.controllers.rekordbox_xml_controller import RekordboxXMLController
from pymix.model.db_tables import PlaylistNodeRow
from pymix.tests.fixtures.playlist_tree import (  # noqa: F401 (fixtures)
    USER, _import, _incoming, _node, _nodes, _outline, db_controller, navidrome, sessions, set_tree_state, tree,
)

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
async def test_a_user_without_a_tree_is_refused_and_nothing_is_written(tree, navidrome, sessions):
    # Since #211 there's no import by joined name. Only demo is 'none', and it can't
    # import (require_uploader).
    set_tree_state(sessions, 'none')

    with pytest.raises(TreeNotEnabled):
        await _import(tree, _incoming('House', 'Deep'), _incoming('Loose'))

    assert navidrome.playlists == {} and _nodes(sessions) == []


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
async def test_a_rekordbox_import_hands_its_playlists_to_the_tree_and_writes_no_path_rows():
    controller = RekordboxXMLController.__new__(RekordboxXMLController)
    controller._db_controller = mock.Mock()
    controller._subsonic_orchestrator = mock.Mock(update_tracks_with_subid=mock.AsyncMock())
    controller._playlist_tree = mock.Mock(import_playlists=mock.AsyncMock(return_value='report'))

    result = await controller._create_playlists_from_xml(
        USER, rekordbox_xml=None, subbox_playlists=[_incoming('House', 'Deep')], scan_finished=False)

    assert result == 'report'
    controller._playlist_tree.import_playlists.assert_awaited_once_with(
        USER, mock.ANY, origin='rekordbox', scan_finished=False)
    # playlist_path_table is gone (#211): the import reads and writes nothing of the user's.
    assert controller._db_controller.method_calls == []
