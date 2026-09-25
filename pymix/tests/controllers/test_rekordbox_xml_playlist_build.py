"""
#191: a large Rekordbox XML must not hold pymix's event loop.

On prod a metadata-only import of a 322-playlist XML walked the whole library
twice, on the loop, and pymix served nobody -- the metrics scrape included --
for 11.5 minutes. The playlists are now built once per import, in a worker
thread, and handed to both passes.
"""
import threading
from unittest import mock

import pytest

from pymix.controllers.rekordbox_xml_controller import RekordboxXMLController
from pymix.model.subboxplaylist import SubBoxPlaylist
from pymix.model.subboxtrack import SubBoxTrack


def _make_controller(rekordbox_xml_orchestrator):
    return RekordboxXMLController(
        subsonic_orchestrator=mock.AsyncMock(),
        rekordbox_xml_orchestrator=rekordbox_xml_orchestrator,
        rb_backup_file_handler=mock.Mock(),
        file_browser_file_handler=mock.Mock(),
        subsonic_client=mock.Mock(),
        db_controller=mock.Mock(),
        wishlist_reconcile_service=mock.Mock(),
        restored_db_output_root="foo",
        local_user_music_stem="foo",
        serving_music_path_base="/private-music",
        beets_exec=mock.Mock(),
    )


@pytest.mark.anyio
async def test_playlists_are_built_once_off_the_event_loop_and_shared_by_both_passes():
    loop_thread = threading.get_ident()
    built_on = []
    playlists = [SubBoxPlaylist(
        name="Sets / Friday", path_components=["Sets", "Friday"],
        tracks=[SubBoxTrack(name="T", artist="A", album="B", track_id=7)],
    )]

    def build(rekordbox_xml, requested):
        built_on.append(threading.get_ident())
        return playlists

    orchestrator = mock.Mock()
    orchestrator.get_subbox_playlists_from_rekordbox_xml.side_effect = build
    orchestrator.get_all_xml_tracks.return_value = []
    controller = _make_controller(orchestrator)
    rekordbox_xml = mock.Mock()
    rekordbox_xml.get_tracks.return_value = []

    with mock.patch.object(controller, "_create_playlists_from_xml", wraps=controller._create_playlists_from_xml) as create, \
         mock.patch.object(controller, "_set_metadata_from_xml", wraps=controller._set_metadata_from_xml) as metadata:
        await controller._set_data_from_xml({"username": "demo"}, rekordbox_xml, [["Sets", "Friday"]])

    orchestrator.get_subbox_playlists_from_rekordbox_xml.assert_called_once_with(rekordbox_xml, [["Sets", "Friday"]])
    assert built_on and built_on[0] != loop_thread
    assert create.call_args.args[4] is playlists
    assert metadata.call_args.args[5] is playlists
    controller._subsonic_orchestrator.create_playlists.assert_awaited_once_with({"username": "demo"}, playlists)
