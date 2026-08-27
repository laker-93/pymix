"""What POST /serato/export hands the client, now that the client writes the crates.

pymix used to write the `.crate` files itself, against a `user_root` the client
sent it -- a prediction about a filesystem the server has never seen. The tests
here pin the two things that replaced it and that nothing else checks:

  * `relative_path` is the track's path *inside the download zip*, so the client
    can find the file it just extracted. Get this wrong and the crates parse
    perfectly and resolve nothing, which is exactly how the old version failed.
  * the crate tree comes from the stored `path_components`, not from splitting
    the display name, so a playlist whose own name contains ' / ' doesn't
    silently become two folders.
"""
from pathlib import Path
from unittest import mock

import pytest

from pymix.controllers.serato_controller import SeratoController
from pymix.model.subboxplaylist import SubBoxPlaylist
from pymix.model.subboxtrack import SubBoxTrack

USER = {'username': 'dj'}
MUSIC_ROOT = Path('/private-music/dj')


def track(relative: str, subbox_id: str = 'sid-1', **kwargs) -> SubBoxTrack:
    """A track as the subsonic client builds it: pymix_path under the music root."""
    return SubBoxTrack(
        name=kwargs.pop('name', 'Title'),
        artist=kwargs.pop('artist', 'Artist'),
        album=kwargs.pop('album', 'Album'),
        # What Navidrome reports, which is NOT under the serving root -- the
        # reason relative_path is taken from pymix_path and not from this.
        path=Path('/music') / relative,
        pymix_path=MUSIC_ROOT / relative,
        subbox_id=subbox_id,
        **kwargs,
    )


@pytest.fixture
def controller():
    subsonic = mock.MagicMock()
    fb = mock.MagicMock()
    fb.get_user_music_root.return_value = MUSIC_ROOT
    db = mock.MagicMock()
    db.get_playlist_paths.return_value = []
    db.get_cuedata_by_subbox_id.return_value = {}

    c = SeratoController(
        subsonic_orchestrator=subsonic,
        serato_crate_orchestrator=mock.MagicMock(),
        serato_backup_file_handler=mock.MagicMock(),
        file_browser_file_handler=fb,
        rb_backup_file_handler=mock.MagicMock(),
        rb_xml_controller=mock.MagicMock(),
        db_controller=db,
        wishlist_reconcile_service=mock.MagicMock(),
        serving_music_path_base='/private-music',
        beets_exec=mock.MagicMock(),
    )
    c.subsonic = subsonic
    c.db = db
    return c


def playlists_are(controller, *playlists):
    controller.subsonic.get_subsonic_playlists = mock.AsyncMock(return_value=list(playlists))


@pytest.mark.anyio
async def test_relative_path_is_where_the_track_lands_in_the_download(controller):
    """The one contract between the halves: <extract>/music/<relative_path>."""
    playlists_are(controller, SubBoxPlaylist(name='House', tracks=[track('Artist/Album/x.mp3')]))

    response = await controller.get_export_structure(USER)

    assert response.crates[0].tracks[0].relative_path == 'Artist/Album/x.mp3'
    assert response.n_tracks == 1
    assert response.n_crates == 1


@pytest.mark.anyio
async def test_a_track_outside_the_music_root_is_left_out_rather_than_guessed_at(controller):
    """It can't be in the zip either, so there is no local path to point a crate at."""
    stray = track('Artist/Album/x.mp3')
    stray.pymix_path = Path('/somewhere/else/x.mp3')
    playlists_are(
        controller,
        SubBoxPlaylist(name='House', tracks=[stray, track('Artist/Album/y.mp3', 'sid-2')]),
    )

    response = await controller.get_export_structure(USER)

    assert [t.relative_path for t in response.crates[0].tracks] == ['Artist/Album/y.mp3']


@pytest.mark.anyio
async def test_the_crate_tree_comes_from_stored_components_not_the_display_name(controller):
    """A playlist whose own name contains ' / ' must not split into two folders."""
    controller.db.get_playlist_paths.return_value = [
        {'display_name': 'Sets / Ambient / Drone', 'path_components': ['Sets', 'Ambient / Drone']},
    ]
    playlists_are(
        controller,
        SubBoxPlaylist(name='Sets / Ambient / Drone', tracks=[track('a/b/c.mp3')]),
    )

    response = await controller.get_export_structure(USER)

    assert response.crates[0].path_components == ['Sets', 'Ambient / Drone']
    assert response.crates[0].display_name == 'Sets / Ambient / Drone'


@pytest.mark.anyio
async def test_a_playlist_with_no_stored_components_still_gets_a_tree(controller):
    """The fallback for playlists made in subbox rather than imported from crates."""
    playlists_are(controller, SubBoxPlaylist(name='Sets / Ambient', tracks=[track('a/b/c.mp3')]))

    response = await controller.get_export_structure(USER)

    assert response.crates[0].path_components == ['Sets', 'Ambient']


@pytest.mark.anyio
async def test_stored_cues_travel_with_the_track(controller):
    controller.db.get_cuedata_by_subbox_id.return_value = {
        'sid-1': {
            'cues': [{'index': 0, 'position': 12000, 'name': 'in'}],
            'loops': [{'index': 0, 'start': 30000, 'end': 38000}],
        }
    }
    playlists_are(controller, SubBoxPlaylist(name='House', tracks=[track('a/b/c.mp3')]))

    response = await controller.get_export_structure(USER)

    cues = response.crates[0].tracks[0].cues
    assert [(c.type, c.start_ms, c.end_ms) for c in cues] == [
        ('cue', 12000, None),
        ('loop', 30000, 38000),
    ]


@pytest.mark.anyio
async def test_cues_are_fetched_in_one_query_for_the_whole_export(controller):
    """A query per track is a thousand round trips on a real library."""
    playlists_are(
        controller,
        SubBoxPlaylist(name='A', tracks=[track('a.mp3', 'sid-1'), track('b.mp3', 'sid-2')]),
        SubBoxPlaylist(name='B', tracks=[track('c.mp3', 'sid-3')]),
    )

    await controller.get_export_structure(USER)

    controller.db.get_cuedata_by_subbox_id.assert_called_once()
    _, ids = controller.db.get_cuedata_by_subbox_id.call_args[0]
    assert sorted(ids) == ['sid-1', 'sid-2', 'sid-3']


@pytest.mark.anyio
async def test_a_playlist_whose_tracks_all_dropped_out_is_not_exported_empty(controller):
    """An empty crate reads to the user as subbox having lost the tracks."""
    stray = track('a.mp3')
    stray.pymix_path = Path('/somewhere/else/a.mp3')
    playlists_are(controller, SubBoxPlaylist(name='House', tracks=[stray]))

    response = await controller.get_export_structure(USER)

    assert response.crates == []
    assert response.success is True


@pytest.mark.anyio
async def test_no_playlists_is_a_success_with_nothing_in_it(controller):
    playlists_are(controller)

    response = await controller.get_export_structure(USER)

    assert response.success is True
    assert response.crates == []
    assert 'no playlists' in response.reason


@pytest.mark.anyio
async def test_requested_playlist_ids_scope_the_fetch_itself(controller):
    """Not a filter after the fact: fetching a playlist's tracks is a round trip."""
    playlists_are(controller, SubBoxPlaylist(name='House', tracks=[track('a.mp3')]))

    await controller.get_export_structure(USER, playlist_ids=['pl-1', 'pl-2'])

    _, id_set = controller.subsonic.get_subsonic_playlists.call_args[0]
    assert id_set == {'pl-1', 'pl-2'}
