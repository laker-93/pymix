"""
#205: the admin migration builds a `none` user's playlist tree from their joined
names, renames each playlist to its leaf, then makes them `live`; its rollback puts
every name back (design-playlists-and-undo §6).

The same real database, tree controller and fake Navidrome as #202 and #204. The
user starts `none`, as every user does before the migration.
"""
import asyncio

import pytest

from pymix.controllers.db_controller import DbController
from pymix.controllers.playlist_tree_controller import TreeInvariantError
from pymix.model.db_tables import PlaylistNodeRow
from pymix.routers.admin import PlaylistMigrateRequest, migrate_playlists
from pymix.tests.fixtures.playlist_tree import (  # noqa: F401 (fixtures)
    USER, _node, _nodes, _outline, _xml, add_batch, db_controller, navidrome, rekordbox, serato, sessions, tree,
)


@pytest.fixture(autouse=True)
def none_user(db_controller):
    db_controller.set_playlist_tree_state('dj', 'none')


def _names(navidrome):
    return sorted(p.name for p in navidrome.playlists.values())


def _library(navidrome, db_controller):
    """What a `none` user has after a Rekordbox import: joined names, and a path row
    for each. `Edits / 2024` is one Rekordbox playlist whose own name has ' / ' in it."""
    ids = {}
    for name in ('House / 2024 / Deep', 'House / 2024 / Tech', 'House / Warmup', 'Loose', 'Edits / 2024'):
        ids[name] = navidrome.add(name, songs=(str(len(ids) + 1),))
    db_controller.save_playlist_paths('dj', [
        {'display_name': 'House / 2024 / Deep', 'path_components': ['House', '2024', 'Deep']},
        {'display_name': 'House / 2024 / Tech', 'path_components': ['House', '2024', 'Tech']},
        {'display_name': 'House / Warmup', 'path_components': ['House', 'Warmup']},
        {'display_name': 'Loose', 'path_components': ['Loose']},
        {'display_name': 'Edits / 2024', 'path_components': ['Edits / 2024']},
    ])
    return ids


@pytest.mark.anyio
async def test_the_migration_builds_the_tree_renames_to_leaves_and_goes_live(tree, navidrome, db_controller, sessions):
    ids = _library(navidrome, db_controller)

    result = await tree.migrate(USER)

    assert result['outcome'] == 'migrated'
    assert db_controller.playlist_tree_state('dj') == 'live'
    assert await _outline(tree) == [
        ('Edits / 2024', 'playlist'),
        ('House', 'folder'),
        ('  2024', 'folder'),
        ('    Deep', 'playlist'),
        ('    Tech', 'playlist'),
        ('  Warmup', 'playlist'),
        ('Loose', 'playlist'),
    ]
    # `Edits / 2024` already is its leaf: its path row says it's one name.
    assert result['renames'] == 3
    assert result['split_without_path_row'] == []
    deep = _node(sessions, ids['House / 2024 / Deep'])
    assert (deep.origin, deep.source_path, deep.migrated_from_name) == (
        'migrated', ['House', '2024', 'Deep'], 'House / 2024 / Deep')


@pytest.mark.anyio
async def test_the_migrated_tree_exports_what_the_joined_names_did(tree, navidrome, db_controller, rekordbox):
    _library(navidrome, db_controller)
    navidrome.add('Sets', songs=('7',))          # a Serato crate with its own tracks...
    navidrome.add('Sets / Peak', songs=('8',))   # ...and a sub-crate, with no path rows
    before = await _xml(rekordbox)

    await tree.migrate(USER)

    assert await _xml(rekordbox) == before


@pytest.mark.anyio
async def test_a_name_with_no_path_row_is_split_and_counted(tree, navidrome):
    # §15 Q8: made in subbox, or by an import from before path rows existed.
    navidrome.add('Ideas / Later', songs=('1',))

    result = await tree.migrate(USER)

    assert result['split_without_path_row'] == ['Ideas / Later']
    assert await _outline(tree) == [('Ideas', 'folder'), ('  Later', 'playlist')]


@pytest.mark.anyio
async def test_a_dry_run_reports_and_writes_nothing(tree, navidrome, db_controller, sessions):
    _library(navidrome, db_controller)
    navidrome.add('Ideas / Later', songs=('9',))
    names = _names(navidrome)

    result = await tree.migrate(USER, dry_run=True)

    assert (result['outcome'], result['playlists'], result['renames']) == ('dry_run', 6, 4)
    assert result['split_without_path_row'] == ['Ideas / Later']
    assert _names(navidrome) == names
    assert _nodes(sessions) == []
    assert db_controller.playlist_tree_state('dj') == 'none'


@pytest.mark.anyio
async def test_a_live_user_is_left_alone(tree, navidrome, db_controller):
    navidrome.add('House / Deep')
    db_controller.set_playlist_tree_state('dj', 'live')

    assert (await tree.migrate(USER))['outcome'] == 'already_live'
    assert navidrome.renamed == []


@pytest.mark.anyio
async def test_other_users_and_smart_playlists_are_not_split_or_renamed(tree, navidrome):
    navidrome.add('Public / Mix', owner='someone')
    smart = navidrome.add('Smart / Top Rated', readonly=True)

    await tree.migrate(USER)

    assert navidrome.renamed == []
    assert await _outline(tree) == [('Smart / Top Rated', 'playlist')]
    assert _node(tree._sessions, smart).source_path == ['Smart / Top Rated']


@pytest.mark.anyio
async def test_a_stopped_migration_stays_none_and_the_next_run_finishes_it(
        tree, navidrome, db_controller, sessions):
    ids = _library(navidrome, db_controller)
    navidrome.refuse_rename = {'Tech'}

    result = await tree.migrate(USER)

    # Deep and Warmup are already renamed; Tech isn't. Nothing shows yet.
    assert result['outcome'] == 'incomplete' and result['failed_renames'] == ['House / 2024 / Tech']
    assert db_controller.playlist_tree_state('dj') == 'none'
    assert navidrome.playlists[ids['House / 2024 / Deep']].name == 'Deep'

    navidrome.refuse_rename = set()
    navidrome.renamed.clear()
    result = await tree.migrate(USER)

    # Deep is still found under House / 2024, not at the root as its leaf name alone
    # would put it, and isn't renamed twice.
    assert result['outcome'] == 'migrated'
    assert navidrome.renamed == [(ids['House / 2024 / Tech'], 'Tech')]
    assert [line for line, _ in await _outline(tree)] == [
        'Edits / 2024', 'House', '  2024', '    Deep', '    Tech', '  Warmup', 'Loose']
    assert _node(sessions, ids['House / 2024 / Deep']).migrated_from_name == 'House / 2024 / Deep'


@pytest.mark.anyio
async def test_the_migration_holds_the_tree_lock_throughout(tree, navidrome, db_controller):
    # An export reads the state under the lock (#204). If it could read it between
    # the renames and the flip to `live`, it would split leaf names that have no tree.
    _library(navidrome, db_controller)
    seen = []
    renaming = navidrome.rename_playlist

    async def slow_rename(user, playlist_id, name):
        await asyncio.sleep(0)
        return await renaming(user, playlist_id, name)
    navidrome.rename_playlist = slow_rename

    migration = asyncio.ensure_future(tree.migrate(USER))
    await asyncio.sleep(0)
    seen.append(await tree.export_tree(USER))
    await migration

    [exported] = seen
    assert exported is not None and [n.name for n in exported][:2] == ['Edits / 2024', 'House']


# --- rollback ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_a_rollback_puts_every_name_back_exactly(tree, navidrome, db_controller, sessions):
    ids = _library(navidrome, db_controller)
    before = {pid: p.name for pid, p in navidrome.playlists.items()}
    await tree.migrate(USER)
    # After the migration, the user renames one and makes one in a folder.
    navidrome.playlists[ids['Loose']].name = 'Loose (old)'
    house = next(n for n in _nodes(sessions) if n.name == 'House')
    new = navidrome.add('Fresh')
    await tree.get_tree(USER)                       # adopts it at the root
    await tree.move_node(USER, _node(sessions, new).node_id, house.node_id)

    result = await tree.rollback(USER)

    assert result['outcome'] == 'rolled_back'
    assert db_controller.playlist_tree_state('dj') == 'none'
    assert _nodes(sessions) == []
    # The migrated ones get exactly their old names, the new one its joined path.
    assert {pid: p.name for pid, p in navidrome.playlists.items()} == {**before, new: 'House / Fresh'}


@pytest.mark.anyio
async def test_a_rolled_back_user_exports_as_before(tree, navidrome, db_controller, rekordbox):
    _library(navidrome, db_controller)
    before = await _xml(rekordbox)
    await tree.migrate(USER)

    await tree.rollback(USER)

    assert await _xml(rekordbox) == before


@pytest.mark.anyio
async def test_a_stopped_rollback_keeps_the_nodes_and_the_next_one_finishes(tree, navidrome, db_controller, sessions):
    ids = _library(navidrome, db_controller)
    await tree.migrate(USER)
    navidrome.refuse_rename = {'House / 2024 / Deep'}

    result = await tree.rollback(USER)

    assert result['outcome'] == 'incomplete'
    assert db_controller.playlist_tree_state('dj') == 'none'
    assert _nodes(sessions)

    navidrome.refuse_rename = set()
    assert (await tree.rollback(USER))['outcome'] == 'rolled_back'
    assert navidrome.playlists[ids['House / 2024 / Deep']].name == 'House / 2024 / Deep'
    assert (await tree.rollback(USER))['outcome'] == 'already_none'


@pytest.mark.anyio
async def test_a_migration_after_a_stopped_rollback_rebuilds_the_same_tree(tree, navidrome, db_controller):
    _library(navidrome, db_controller)
    await tree.migrate(USER)
    migrated = await _outline(tree)
    navidrome.refuse_rename = {'House / Warmup'}
    await tree.rollback(USER)
    navidrome.refuse_rename = set()

    assert (await tree.migrate(USER))['outcome'] == 'migrated'
    assert await _outline(tree) == migrated


@pytest.mark.anyio
async def test_a_rollback_is_refused_once_anything_is_in_the_trash(tree, navidrome, db_controller, sessions):
    _library(navidrome, db_controller)
    await tree.migrate(USER)
    with sessions() as session:
        add_batch(session, 'b')
        session.query(PlaylistNodeRow).filter(PlaylistNodeRow.name == 'House').update({'trash_batch_id': 'b'})
        session.commit()

    with pytest.raises(TreeInvariantError):
        await tree.rollback(USER)
    assert db_controller.playlist_tree_state('dj') == 'live'


# --- every user ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_the_all_users_run_reports_each_user_and_how_many_are_still_none(tree, navidrome):
    navidrome.add('House / Deep')

    result = await tree.migrate_all([USER, {'username': 'gone', 'password': 'pw'}])

    assert [(u['username'], u['outcome']) for u in result['users']] == [('dj', 'migrated'), ('gone', 'error')]
    assert result['still_none'] == 1


# --- the admin route ------------------------------------------------------------------------

@pytest.mark.anyio
@pytest.mark.parametrize('body, status', [
    ({}, 400),
    ({'username': 'dj', 'all_users': True}, 400),
    ({'all_users': True, 'rollback': True}, 400),
    ({'username': 'dj', 'rollback': True, 'dry_run': True}, 400),
    ({'username': 'demo'}, 400),
    ({'username': 'nobody'}, 404),
])
async def test_the_route_refuses_what_it_should(tree, db_controller, body, status):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as raised:
        await migrate_playlists(PlaylistMigrateRequest(**body), db_controller=db_controller, tree=tree)
    assert raised.value.status_code == status


@pytest.mark.anyio
async def test_the_route_rolls_back_a_trashed_tree_as_409(tree, navidrome, db_controller, sessions):
    from fastapi import HTTPException
    navidrome.add('House / Deep')
    await tree.migrate(USER)
    with sessions() as session:
        add_batch(session, 'b')
        session.query(PlaylistNodeRow).update({'trash_batch_id': 'b'})
        session.commit()

    with pytest.raises(HTTPException) as raised:
        await migrate_playlists(PlaylistMigrateRequest(username='dj', rollback=True),
                                db_controller=db_controller, tree=tree)
    assert raised.value.status_code == 409


# --- new users --------------------------------------------------------------------------------

@pytest.mark.parametrize('configured, state', [(None, 'none'), ('none', 'none'), ('live', 'live')])
def test_a_new_user_starts_in_the_configured_state(sessions, tmp_path, configured, state):
    from pymix.model.db_tables import UserTokenRow
    with sessions() as session:
        session.add(UserTokenRow(user_id='', token='t'))
        session.commit()
    db = DbController(session_factory=sessions, app_env='test', max_library_size=0,
                      serving_music_path_base=str(tmp_path), new_user_playlist_tree_state=configured)

    db.create_user('newbie', 'password123456', 'n@example.com', 't')

    assert db.playlist_tree_state('newbie') == state
