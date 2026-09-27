"""
#204: a `live` user's Rekordbox and Serato exports walk the playlist tree, so what
an import built comes back out with the same structure and in the user's own order
(design-playlists-and-undo §5.4, §5.5). A `none` user's export is unchanged.

The tree is built by a real import (#202) over the fake Navidrome; the Rekordbox XML
is real, minus the audio files each track would need.
"""
import asyncio
from pathlib import Path
from unittest import mock

import pytest
from pyrekordbox.rbxml import RekordboxXml

from pymix.controllers.rekordbox_xml_controller import RekordboxXMLController
from pymix.controllers.serato_controller import SeratoController
from pymix.model.db_tables import PlaylistNodeRow
from pymix.model.subboxtrack import SubBoxTrack
from pymix.tests.fixtures.playlist_tree import (  # noqa: F401 (fixtures)
    USER, FakeNavidrome, _import, _incoming, _node, db_controller, sessions, tree,
)

MUSIC_ROOT = Path('/private-music/dj')


class ExportNavidrome(FakeNavidrome):
    async def get_playlist_tracks(self, user, playlist_id):
        # Under the music root, so the Serato export can place each one in the zip.
        return [SubBoxTrack(name=s, artist='a', album='b', sub_track_id=s, subbox_id=f'sid-{s}',
                            pymix_path=MUSIC_ROOT / f'{s}.mp3')
                for s in self.entries[playlist_id]]


@pytest.fixture
def navidrome():
    return ExportNavidrome()


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


NESTED = [('House', '2024', 'Deep'), ('House', '2024', 'Tech'), ('House', 'Warmup'), ('Loose',)]


# --- Rekordbox ----------------------------------------------------------------------

@pytest.mark.anyio
async def test_a_nested_rekordbox_import_exports_with_the_same_structure_and_order(tree, rekordbox):
    await _import(tree, *(_incoming(*path, songs=(str(i), str(i + 10))) for i, path in enumerate(NESTED)))

    lines = await _xml(rekordbox)

    assert lines == [
        ('House', 'folder', None),
        ('  2024', 'folder', None),
        ('    Deep', 'playlist', [0, 10]),
        ('    Tech', 'playlist', [1, 11]),
        ('  Warmup', 'playlist', [2, 12]),
        ('Loose', 'playlist', [3, 13]),
        ('NOPLAYLIST', 'playlist', []),
    ]
    assert [path for path, _ in _playlists(lines)][:-1] == [list(p) for p in NESTED]


@pytest.mark.anyio
async def test_the_users_sibling_order_reaches_rekordbox(tree, rekordbox, sessions):
    await _import(tree, _incoming('B', songs=('1',)), _incoming('A', songs=('2',)), _incoming('C', songs=('3',)))
    c = _node(sessions, next(pid for pid, p in tree._subsonic._subsonic_client.playlists.items() if p.name == 'C'))

    await tree.move_node(USER, c.node_id, None, 0)

    # Not alphabetical, which is all the sort by name ever gave.
    assert [name for name, *_ in await _xml(rekordbox)] == ['C', 'B', 'A', 'NOPLAYLIST']


@pytest.mark.anyio
async def test_a_playlist_with_children_goes_out_as_itself_then_a_folder_and_comes_back_whole(
        tree, rekordbox, serato, navidrome, sessions):
    await _import(
        tree,
        _incoming('Sets', 'House', songs=('1',)),
        _incoming('Sets', 'House', 'Deep', songs=('2',)),
        _incoming('Sets', 'Techno', 'Peak', songs=('3',)),
        origin='serato',
    )

    lines = await _xml(rekordbox)

    assert lines == [
        ('Sets', 'folder', None),
        ('  House', 'playlist', [1]),
        ('  House', 'folder', None),
        ('    Deep', 'playlist', [2]),
        ('  Techno', 'folder', None),
        ('    Peak', 'playlist', [3]),
        ('NOPLAYLIST', 'playlist', []),
    ]

    # That XML into an emptied account, and back out as crates: the Serato round
    # trip. A folder holding a same-named playlist would come back as `House/House`.
    navidrome.playlists.clear()
    with sessions() as session:
        session.query(PlaylistNodeRow).delete()
        session.commit()
    await _import(tree, *(_incoming(*path, songs=tuple(str(t) for t in tracks))
                          for path, tracks in _playlists(lines) if path != ['NOPLAYLIST']))
    response = await serato.get_export_structure(USER)

    assert [(c.path_components, [t.relative_path for t in c.tracks]) for c in response.crates] == [
        (['Sets', 'House'], ['1.mp3']),
        (['Sets', 'House', 'Deep'], ['2.mp3']),
        (['Sets', 'Techno', 'Peak'], ['3.mp3']),
    ]


@pytest.mark.anyio
async def test_a_trashed_folder_and_everything_under_it_are_left_out(tree, rekordbox, sessions, navidrome):
    await _import(tree, *(_incoming(*path, songs=('1',)) for path in NESTED))
    with sessions() as session:
        # Only the folder: its live children are unreachable through it.
        session.query(PlaylistNodeRow).filter(PlaylistNodeRow.name == '2024').update({'trash_batch_id': 'batch-1'})
        session.commit()

    assert [name for name, *_ in await _xml(rekordbox)] == ['House', '  Warmup', 'Loose', 'NOPLAYLIST']
    # Nothing was deleted: the trashed playlists are still in Navidrome.
    assert len(navidrome.playlists) == 4


@pytest.mark.anyio
async def test_a_renamed_playlist_is_exported_under_its_navidrome_name(tree, rekordbox, navidrome):
    await _import(tree, _incoming('House', 'Deep', songs=('1',)))
    [playlist] = navidrome.playlists.values()
    playlist.name = 'Deep (Sunday)'

    assert (await _xml(rekordbox))[:2] == [('House', 'folder', None), ('  Deep (Sunday)', 'playlist', [1])]


@pytest.mark.anyio
async def test_an_empty_folder_is_exported(tree, rekordbox):
    await tree.create_node(USER, 'folder', name='Ideas')

    assert await _xml(rekordbox) == [('Ideas', 'folder', None), ('NOPLAYLIST', 'playlist', [])]


@pytest.mark.anyio
async def test_a_filtered_export_has_the_selected_playlists_and_their_path_only(tree, rekordbox, navidrome):
    await _import(
        tree,
        *(_incoming(*path, songs=('1',)) for path in NESTED),
        _incoming('Sets', 'House', songs=('2',)),
        _incoming('Sets', 'House', 'Deep', songs=('3',)),
        _incoming('Sets', 'Techno', songs=('4',)),
    )
    by_path = {tuple(_node(tree._sessions, pid).source_path): pid for pid in navidrome.playlists}

    lines = await _xml(rekordbox, [by_path[('House', '2024', 'Tech')], by_path[('Sets', 'House', 'Deep')]])

    # Sets/House is on the path to Deep but wasn't selected: a folder, without its
    # own playlist. Deep under House/2024, Warmup, Loose, Techno: not selected. No
    # NOPLAYLIST either.
    assert lines == [
        ('House', 'folder', None),
        ('  2024', 'folder', None),
        ('    Tech', 'playlist', [1]),
        ('Sets', 'folder', None),
        ('  House', 'folder', None),
        ('    Deep', 'playlist', [3]),
    ]


@pytest.mark.anyio
async def test_a_none_users_export_is_unchanged(tree, rekordbox, navidrome, db_controller):
    db_controller.set_playlist_tree_state('dj', 'none')
    navidrome.add('House / Deep', songs=('1',))
    navidrome.add('Loose', songs=('2',))

    assert await tree.export_tree(USER) is None
    assert await _xml(rekordbox) == [
        ('House', 'folder', None),
        ('  Deep', 'playlist', [1]),
        ('Loose', 'playlist', [2]),
        ('NOPLAYLIST', 'playlist', []),
    ]


@pytest.mark.anyio
async def test_the_state_is_read_under_the_tree_lock(tree, navidrome, db_controller):
    # #205's migration holds the lock while it renames a user's playlists and then
    # makes them `live`. An export that read the state first would take them for a
    # `none` user and split names that are no longer joined.
    db_controller.set_playlist_tree_state('dj', 'none')
    navidrome.add('Deep', songs=('1',))
    async with tree._locks.hold('dj'):
        export = asyncio.ensure_future(tree.export_tree(USER))
        await asyncio.sleep(0)
        db_controller.set_playlist_tree_state('dj', 'live')

    exported = await export
    assert [n.name for n in exported] == ['Deep']


# --- Serato ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_serato_crates_take_their_path_from_the_tree_parents_first(tree, serato, sessions, navidrome):
    await _import(
        tree,
        _incoming('Sets', 'House', songs=('1',)),
        _incoming('Sets', 'House', 'Deep', songs=('2',)),
        _incoming('Sets', 'Techno', 'Peak', songs=('3',)),
        _incoming('Loose', songs=('4',)),
        origin='serato',
    )
    await tree.create_node(USER, 'folder', name='Ideas')
    [loose] = [pid for pid, p in navidrome.playlists.items() if p.name == 'Loose']
    await tree.move_node(USER, _node(sessions, loose).node_id, None, 0)

    response = await serato.get_export_structure(USER)

    # A folder is only a path; one with no playlist under it has no crate.
    assert [(c.path_components, [t.relative_path for t in c.tracks]) for c in response.crates] == [
        (['Loose'], ['4.mp3']),
        (['Sets', 'House'], ['1.mp3']),
        (['Sets', 'House', 'Deep'], ['2.mp3']),
        (['Sets', 'Techno', 'Peak'], ['3.mp3']),
    ]
    assert [c.display_name for c in response.crates] == ['Loose', 'House', 'Deep', 'Peak']


@pytest.mark.anyio
async def test_a_filtered_serato_export_has_the_selected_crates_only(tree, serato, navidrome):
    await _import(
        tree,
        _incoming('Sets', 'House', songs=('1',)),
        _incoming('Sets', 'House', 'Deep', songs=('2',)),
        _incoming('Loose', songs=('3',)),
        origin='serato',
    )
    [deep] = [pid for pid, p in navidrome.playlists.items() if p.name == 'Deep']

    response = await serato.get_export_structure(USER, [deep])

    assert [c.path_components for c in response.crates] == [['Sets', 'House', 'Deep']]


@pytest.mark.anyio
async def test_a_none_users_serato_export_still_splits_joined_names(tree, serato, navidrome, db_controller):
    db_controller.set_playlist_tree_state('dj', 'none')
    navidrome.add('Sets / House', songs=('1',))

    response = await serato.get_export_structure(USER)

    assert [c.path_components for c in response.crates] == [['Sets', 'House']]
