"""
#229: a playlist's Navidrome name is written from the tree -- its leaf, or for a
`path` user its full path -- so third-party Subsonic clients see the folders
(design-playlists-and-undo §18). And a rename made outside pymix is read back, as
long as it names a leaf in the same place; one naming another place waits for #230.

Real database (SQLite), the real tree controller and Subsonic orchestrator, and the
fake Navidrome the import and export tests use.
"""
import pytest

from pymix.controllers.playlist_tree_controller import TreeInvariantError
from pymix.model.db_tables import PlaylistNodeRow, UserRow
from pymix.services import metrics, playlist_names
from pymix.tests.fixtures.playlist_tree import (  # noqa: F401 (fixtures)
    USER, _import, _incoming, _outline, db_controller, navidrome, sessions, tree,
)


def _outside(outcome):
    return metrics.playlists_renamed_outside_total.labels(outcome=outcome)._value.get()


def _names(navidrome):
    return sorted(p.name for p in navidrome.playlists.values())


def _row(sessions, playlist_id):
    with sessions() as session:
        return session.query(PlaylistNodeRow).filter(PlaylistNodeRow.navidrome_playlist_id == playlist_id).one()


async def _house_deep(tree):
    """House > Deep (a playlist) and Warm (a playlist in Deep's place's sibling
    folder Deep Stuff), plus Loose at the root."""
    house = await tree.create_folder(USER, 'House')
    stuff = await tree.create_folder(USER, 'Deep Stuff', parent_id=house['node_id'])
    deep = await tree.create_playlist(USER, 'Deep', parent_id=house['node_id'])
    warm = await tree.create_playlist(USER, 'Warm', parent_id=stuff['node_id'])
    loose = await tree.create_playlist(USER, 'Loose')
    return house, stuff, deep, warm, loose


async def _to_path(tree):
    return await tree.set_name_style(USER, 'path')


# --- the name ---------------------------------------------------------------------------

@pytest.mark.parametrize('names, joined', [
    (['House'], 'House'),
    (['Bass', 'House'], 'Bass / House'),
    (['Bass', 'Drum / Bass'], 'Bass / Drum ∕ Bass'),
    # Only " / " is the separator: a slash without spaces is an ordinary character.
    (['AC/DC', 'Live'], 'AC/DC / Live'),
])
def test_a_path_is_joined_with_a_literal_separator_escaped(names, joined):
    assert playlist_names.join(names) == joined


@pytest.mark.parametrize('name, prefix, leaf', [
    ('Deeper', ['House'], 'Deeper'),
    ('House / Deeper', ['House'], 'Deeper'),
    ('House / Drum ∕ Bass', ['House'], 'Drum / Bass'),
    ('Bass / Deep', ['House'], None),
    ('House / Sub / Deep', ['House'], None),
    ('House / Deep', [], None),
])
def test_a_name_gives_a_leaf_only_in_the_same_place(name, prefix, leaf):
    assert playlist_names.leaf_under(name, prefix) == leaf


# --- moving a user between styles --------------------------------------------------------

@pytest.mark.anyio
async def test_everyone_starts_on_leaf_names_and_nothing_is_renamed(tree, navidrome):
    await _house_deep(tree)
    await tree.get_tree(USER)

    assert _names(navidrome) == ['Deep', 'Loose', 'Warm']
    assert navidrome.renamed == []


@pytest.mark.anyio
async def test_the_admin_pass_names_every_playlist_by_its_path_and_back(tree, navidrome, sessions):
    await _house_deep(tree)

    dry = await tree.set_name_style(USER, 'path', dry_run=True)
    assert (dry['owed'], dry['renamed'], navidrome.renamed) == (2, 0, [])
    assert sorted(r['to'] for r in dry['renames']) == ['House / Deep', 'House / Deep Stuff / Warm']
    with sessions() as session:
        assert session.query(UserRow.playlist_names).scalar() == 'leaf'

    done = await _to_path(tree)
    assert (done['from'], done['to'], done['renamed'], done['failed']) == ('leaf', 'path', 2, [])
    assert _names(navidrome) == ['House / Deep', 'House / Deep Stuff / Warm', 'Loose']
    # Idempotent.
    assert (await _to_path(tree))['owed'] == 0
    # The tree, and so the sidebar, still has leaves.
    assert await _outline(tree) == [
        ('House', 'folder'), ('  Deep Stuff', 'folder'), ('    Warm', 'playlist'), ('  Deep', 'playlist'),
        ('Loose', 'playlist')]

    back = await tree.set_name_style(USER, 'leaf')
    assert back['renamed'] == 2
    assert _names(navidrome) == ['Deep', 'Loose', 'Warm']


@pytest.mark.anyio
async def test_an_unknown_style_is_refused(tree):
    with pytest.raises(TreeInvariantError, match='unknown playlist name style'):
        await tree.set_name_style(USER, 'full')


@pytest.mark.anyio
async def test_a_smart_playlist_keeps_its_own_name(tree, navidrome):
    house = await tree.create_folder(USER, 'House')
    smart = navidrome.add('Recently added', readonly=True)
    await tree.get_tree(USER)
    await tree.move_node(USER, _row_id(tree, smart), house['node_id'])

    await _to_path(tree)

    assert _names(navidrome) == ['Recently added']


def _row_id(tree, playlist_id):
    with tree._sessions() as session:
        return session.query(PlaylistNodeRow.node_id).filter(
            PlaylistNodeRow.navidrome_playlist_id == playlist_id).scalar()


# --- tree writes, for a path user ----------------------------------------------------------

@pytest.mark.anyio
async def test_a_playlist_made_in_a_folder_is_created_under_its_path(tree, navidrome):
    await _to_path(tree)
    house = await tree.create_folder(USER, 'House')

    node = await tree.create_playlist(USER, 'Drum / Bass', parent_id=house['node_id'])

    assert node['name'] == 'Drum / Bass'
    assert _names(navidrome) == ['House / Drum ∕ Bass']
    # Created under that name, not renamed to it afterwards.
    assert navidrome.renamed == []


@pytest.mark.anyio
async def test_renaming_or_moving_a_folder_renames_every_playlist_under_it(tree, navidrome):
    house, stuff, deep, warm, loose = await _house_deep(tree)
    await _to_path(tree)

    await tree.update_node(USER, house['node_id'], name='Bass')
    assert _names(navidrome) == ['Bass / Deep', 'Bass / Deep Stuff / Warm', 'Loose']

    await tree.move_node(USER, stuff['node_id'], None)
    assert _names(navidrome) == ['Bass / Deep', 'Deep Stuff / Warm', 'Loose']


@pytest.mark.anyio
async def test_renaming_a_playlist_writes_its_path(tree, navidrome, sessions):
    house, stuff, deep, warm, loose = await _house_deep(tree)
    await _to_path(tree)

    body = await tree.update_node(USER, deep['node_id'], name='Deeper')

    assert body['name'] == 'Deeper'
    assert navidrome.playlists[deep['navidrome_playlist_id']].name == 'House / Deeper'
    assert _row(sessions, deep['navidrome_playlist_id']).navidrome_name == 'House / Deeper'


@pytest.mark.anyio
async def test_a_rename_that_fails_part_way_is_finished_by_the_next_read(tree, navidrome):
    house, stuff, deep, warm, loose = await _house_deep(tree)
    await _to_path(tree)
    navidrome.refuse_rename = {warm['navidrome_playlist_id']}

    await tree.update_node(USER, house['node_id'], name='Bass')
    assert _names(navidrome) == ['Bass / Deep', 'House / Deep Stuff / Warm', 'Loose']

    navidrome.refuse_rename = set()
    await tree.get_tree(USER)
    # Not mistaken for a rename outside pymix: the tree wins, and Warm stays put.
    assert _names(navidrome) == ['Bass / Deep', 'Bass / Deep Stuff / Warm', 'Loose']


@pytest.mark.anyio
async def test_a_rename_written_but_never_committed_is_recognised_as_pymixs_own(tree, navidrome, sessions):
    house, stuff, deep, warm, loose = await _house_deep(tree)
    await _to_path(tree)
    # pymix died between Navidrome's rename and its own commit.
    with sessions() as session:
        session.query(PlaylistNodeRow).filter(PlaylistNodeRow.node_id == house['node_id']).update({'name': 'Bass'})
        session.commit()
    navidrome.playlists[deep['navidrome_playlist_id']].name = 'Bass / Deep'
    outside = [_outside(k) for k in ('renamed', 'moved', 'refused')]

    await tree.get_tree(USER)

    assert _row(sessions, deep['navidrome_playlist_id']).navidrome_name == 'Bass / Deep'
    assert [_outside(k) for k in ('renamed', 'moved', 'refused')] == outside
    assert _names(navidrome) == ['Bass / Deep', 'Bass / Deep Stuff / Warm', 'Loose']


@pytest.mark.anyio
async def test_an_import_names_what_it_creates_by_its_path(tree, navidrome):
    await _to_path(tree)

    await _import(tree, _incoming('Genre', 'House', 'Deep'), _incoming('Loose'))

    assert _names(navidrome) == ['Genre / House / Deep', 'Loose']


@pytest.mark.anyio
async def test_a_restored_playlist_takes_the_path_of_where_it_went_back(tree, navidrome):
    house, stuff, deep, warm, loose = await _house_deep(tree)
    await _to_path(tree)
    deleted = await tree.delete_nodes(USER, [deep['node_id']])
    await tree.update_node(USER, house['node_id'], name='Bass')
    # Hidden, so not renamed with its folder.
    assert navidrome.playlists[deep['navidrome_playlist_id']].name == 'House / Deep'

    await tree.restore_nodes(USER, deleted['trash_batch_id'])

    assert navidrome.playlists[deep['navidrome_playlist_id']].name == 'Bass / Deep'


@pytest.mark.anyio
async def test_an_export_writes_the_leaf(tree):
    house = await tree.create_folder(USER, 'House')
    await tree.create_playlist(USER, 'Drum / Bass', parent_id=house['node_id'])
    await _to_path(tree)

    [root] = await tree.export_tree(USER)

    assert (root.name, [c.name for c in root.children]) == ('House', ['Drum / Bass'])


# --- renames made outside pymix --------------------------------------------------------------

@pytest.mark.anyio
async def test_a_bare_name_from_an_old_client_renames_the_leaf_in_place(tree, navidrome, sessions):
    house, stuff, deep, warm, loose = await _house_deep(tree)
    await _to_path(tree)
    # Upstream Feishin's edit modal only knows the leaf.
    navidrome.playlists[deep['navidrome_playlist_id']].name = 'Deeper'
    outside = _outside('renamed')

    body = await tree.get_tree(USER)

    assert _outside('renamed') == outside + 1

    assert [n['name'] for n in body['nodes'] if n['node_id'] == deep['node_id']] == ['Deeper']
    assert navidrome.playlists[deep['navidrome_playlist_id']].name == 'House / Deeper'


@pytest.mark.anyio
async def test_a_new_leaf_under_the_same_path_renames_the_leaf(tree, navidrome):
    house, stuff, deep, warm, loose = await _house_deep(tree)
    await _to_path(tree)
    navidrome.playlists[warm['navidrome_playlist_id']].name = 'House / Deep Stuff / Warmer'
    before = list(navidrome.renamed)

    assert ('    Warmer', 'playlist') in await _outline(tree)
    assert navidrome.renamed == before


# --- moves made outside pymix (#230) ----------------------------------------------------------

@pytest.mark.anyio
async def test_a_path_elsewhere_moves_the_playlist_there_creating_the_folder(tree, navidrome, sessions):
    house, stuff, deep, warm, loose = await _house_deep(tree)
    await _to_path(tree)
    navidrome.playlists[deep['navidrome_playlist_id']].name = 'Bass / Deeper'
    moved = _outside('moved')

    await tree.get_tree(USER)

    assert await _outline(tree) == [
        ('House', 'folder'), ('  Deep Stuff', 'folder'), ('    Warm', 'playlist'),
        ('Loose', 'playlist'), ('Bass', 'folder'), ('  Deeper', 'playlist')]
    assert _outside('moved') == moved + 1
    with sessions() as session:
        bass = session.query(PlaylistNodeRow).filter(PlaylistNodeRow.name == 'Bass').one()
        assert (bass.kind, bass.origin, bass.source_path) == ('folder', 'subbox', None)
    # Already the name the tree gives it: nothing to write back.
    assert navidrome.playlists[deep['navidrome_playlist_id']].name == 'Bass / Deeper'


@pytest.mark.anyio
async def test_a_path_to_an_existing_folder_moves_it_to_the_end_of_that_folder(tree, navidrome):
    house, stuff, deep, warm, loose = await _house_deep(tree)
    await _to_path(tree)
    navidrome.playlists[loose['navidrome_playlist_id']].name = 'House / Deep Stuff / Loose'

    assert await _outline(tree) == [
        ('House', 'folder'), ('  Deep Stuff', 'folder'), ('    Warm', 'playlist'), ('    Loose', 'playlist'),
        ('  Deep', 'playlist')]


@pytest.mark.anyio
async def test_an_escaped_slash_is_one_name_renamed_in_place(tree, navidrome):
    house, stuff, deep, warm, loose = await _house_deep(tree)
    await _to_path(tree)
    # No separator, so a bare name: a leaf rename where it is, not a folder "Drum".
    # A move to the root can't be written from outside (design §18.5).
    navidrome.playlists[warm['navidrome_playlist_id']].name = 'Drum \u2215 Bass'

    outline = await _outline(tree)

    assert ('    Drum / Bass', 'playlist') in outline and ('Drum', 'folder') not in outline
    assert navidrome.playlists[warm['navidrome_playlist_id']].name == 'House / Deep Stuff / Drum \u2215 Bass'


@pytest.mark.anyio
async def test_a_playlist_made_with_a_path_is_adopted_along_it(tree, navidrome):
    await _house_deep(tree)
    await _to_path(tree)
    garage = navidrome.add('UK / Garage')

    await tree.get_tree(USER)
    again = await _outline(tree)

    assert again[-2:] == [('UK', 'folder'), ('  Garage', 'playlist')]
    assert navidrome.playlists[garage].name == 'UK / Garage'


@pytest.mark.anyio
async def test_a_path_resolves_through_a_crate_with_sub_crates(tree, navidrome):
    await _import(tree, _incoming('Crate'), _incoming('Crate', 'Sub'), origin='serato')
    loose = await tree.create_playlist(USER, 'Loose')
    await _to_path(tree)
    navidrome.playlists[loose['navidrome_playlist_id']].name = 'Crate / Loose'

    assert await _outline(tree) == [('Crate', 'playlist'), ('  Sub', 'playlist'), ('  Loose', 'playlist')]


@pytest.mark.anyio
@pytest.mark.parametrize('name', ['Crate / Sub / Crate', 'Crate / Crate', ' / Crate', 'Somewhere /  '])
async def test_a_path_into_its_own_subtree_or_with_an_empty_name_is_refused(tree, navidrome, name):
    await _import(tree, _incoming('Crate'), _incoming('Crate', 'Sub'), origin='serato')
    await _to_path(tree)
    [crate] = [pid for pid, p in navidrome.playlists.items() if p.name == 'Crate']
    navidrome.playlists[crate].name = name
    refused = _outside('refused')

    outline = await _outline(tree)

    assert outline == [('Crate', 'playlist'), ('  Sub', 'playlist')]
    assert _outside('refused') == refused + 1
    # Written back from the tree, and nothing created.
    assert navidrome.playlists[crate].name == 'Crate'


@pytest.mark.anyio
async def test_a_hidden_playlist_renamed_outside_is_not_moved(tree, navidrome):
    house, stuff, deep, warm, loose = await _house_deep(tree)
    await _to_path(tree)
    await tree.delete_nodes(USER, [deep['node_id']])
    navidrome.playlists[deep['navidrome_playlist_id']].name = 'Bass / Deep'

    assert ('Bass', 'folder') not in await _outline(tree)


@pytest.mark.anyio
async def test_a_move_empties_a_folder_and_the_folder_stays(tree, navidrome):
    house, stuff, deep, warm, loose = await _house_deep(tree)
    await _to_path(tree)
    navidrome.playlists[warm['navidrome_playlist_id']].name = 'Warm'
    navidrome.playlists[deep['navidrome_playlist_id']].name = 'Deep'

    # Bare names are leaf renames in place (#229), not moves to the root.
    assert await _outline(tree) == [
        ('House', 'folder'), ('  Deep Stuff', 'folder'), ('    Warm', 'playlist'), ('  Deep', 'playlist'),
        ('Loose', 'playlist')]
    navidrome.playlists[warm['navidrome_playlist_id']].name = 'House / Warm'
    outline = await _outline(tree)
    assert ('  Deep Stuff', 'folder') in outline and ('  Warm', 'playlist') in outline


@pytest.mark.anyio
async def test_a_leaf_user_takes_any_outside_name_as_the_leaf(tree, navidrome):
    house, stuff, deep, warm, loose = await _house_deep(tree)
    navidrome.playlists[deep['navidrome_playlist_id']].name = 'Drum / Bass'

    assert ('  Drum / Bass', 'playlist') in await _outline(tree)
    assert navidrome.renamed == []
