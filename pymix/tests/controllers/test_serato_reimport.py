"""
#203 on the Serato side: the import waits for Navidrome's scan (it used to sleep a
flat 2s and never knew whether the scan had finished), and whether it finished
decides whether the user's existing playlists are updated. What the write did to
them travels back on the crate report, into the job's warnings.
"""
from unittest import mock

import pytest

from pymix.controllers.serato_controller import SeratoController
from pymix.model.playlist_write_report import PlaylistWriteReport
from pymix.model.serato_import import CrateImportReport


def _controller(scan_finished):
    orchestrator = mock.Mock()
    orchestrator.scan_and_wait = mock.AsyncMock(return_value=scan_finished)
    orchestrator.update_tracks_with_subid = mock.AsyncMock()
    tree = mock.Mock(import_playlists=mock.AsyncMock(return_value=PlaylistWriteReport(held_back=['Deep'])))
    crates = mock.Mock()
    crates.get_subbox_playlists_from_crates.return_value = ([], CrateImportReport(crates_parsed=1))
    controller = SeratoController(
        subsonic_orchestrator=orchestrator,
        serato_crate_orchestrator=crates,
        serato_backup_file_handler=mock.Mock(),
        file_browser_file_handler=mock.Mock(),
        rb_backup_file_handler=mock.Mock(),
        rb_xml_controller=mock.Mock(),
        db_controller=mock.MagicMock(),
        wishlist_reconcile_service=mock.Mock(reconcile_user=mock.AsyncMock()),
        serving_music_path_base="foo",
        beets_exec=mock.Mock(),
        playlist_tree_controller=tree,
    )
    controller._set_metadata = mock.AsyncMock()
    return controller, orchestrator, tree


@pytest.mark.anyio
@pytest.mark.parametrize('scan_finished', [True, False])
async def test_the_scan_is_waited_for_and_its_result_decides_the_playlist_write(scan_finished):
    controller, orchestrator, tree = _controller(scan_finished)

    report = await controller.create_subsonic_playlists_from_crates(
        user={'username': 'dj'}, serato_crate_path=mock.Mock(), zip_path=None, audio_path=None,
    )

    orchestrator.scan_and_wait.assert_awaited_once()
    orchestrator.scan.assert_not_called()
    assert tree.import_playlists.await_args.kwargs == {'origin': 'serato', 'scan_finished': scan_finished}
    assert report.playlists.held_back == ['Deep']
    assert "`Deep` not updated" in report.warning()
