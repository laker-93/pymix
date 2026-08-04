"""
Tests for the two RekordboxXMLController additions the automatch sweep (#79)
needs: an explicit id-keyed subbox_id remap (rather than
`_map_subbox_id_beet_id`'s `subbox_id::^$` query, which skips already-mapped
tracks) and a synchronous entry point into duplicate re-tagging.

Uses a real BeetsExec (so write_lock genuinely works) with the underlying
docker.execute call patched, same pattern as test_beets_import_command.py.
"""
from pathlib import Path
from unittest import mock

from pymix.clients.beets_exec import BeetsExec
from pymix.controllers.rekordbox_xml_controller import RekordboxXMLController


def _make_controller(beets_exec, db_controller=None):
    return RekordboxXMLController(
        subsonic_orchestrator=mock.Mock(),
        rekordbox_xml_orchestrator=mock.Mock(),
        rb_backup_file_handler=mock.Mock(),
        file_browser_file_handler=mock.Mock(),
        subsonic_client=mock.Mock(),
        db_controller=db_controller or mock.Mock(),
        wishlist_reconcile_service=mock.Mock(),
        restored_db_output_root="foo",
        local_user_music_stem="foo",
        serving_music_path_base="/private-music",
        beets_exec=beets_exec,
    ), db_controller


def test_remap_subbox_id_for_ids_is_a_noop_for_an_empty_list():
    beets_exec = BeetsExec()
    controller, _ = _make_controller(beets_exec)

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker:
        controller.remap_subbox_id_for_ids("demoadmin", [], public=False)

    mock_docker.execute.assert_not_called()


def test_remap_subbox_id_for_ids_queries_by_explicit_ids_and_remaps_each():
    beets_exec = BeetsExec()
    db_controller = mock.Mock()
    controller, _ = _make_controller(beets_exec, db_controller=db_controller)

    def fake_execute(container_name, command, stream=False):
        cmd = command if isinstance(command, list) else command.split()
        if cmd[:3] == ["beet", "list", "-f"]:
            assert cmd == ["beet", "list", "-f", "$id:$path", "id:1", ",", "id:2"]
            return "1:/music/Artist/Album/one.mp3\n2:/music/Artist/Album/two.mp3\n"
        # `beet modify ... subbox_id=...`, executed with stream=True (matches
        # _map_subbox_id_beet_id's existing pattern).
        return iter([])

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker, \
         mock.patch("pymix.controllers.rekordbox_xml_controller.get_subbox_id") as mock_get_subbox_id, \
         mock.patch.object(Path, "exists", return_value=True):
        mock_docker.execute.side_effect = fake_execute
        mock_get_subbox_id.side_effect = ["SBX-1", "SBX-2"]

        controller.remap_subbox_id_for_ids("demoadmin", [1, 2], public=False)

    assert db_controller.add_subbox_beet_map.call_args_list == [
        mock.call(username="demoadmin", subbox_id="SBX-1", beet_id=1),
        mock.call(username="demoadmin", subbox_id="SBX-2", beet_id=2),
    ]
    # BeetsExec.execute splits a str command into a list before it reaches
    # docker.execute (see test_beets_exec.py), so these arrive pre-split.
    modify_calls = [
        c.args[1] for c in mock_docker.execute.call_args_list
        if isinstance(c.args[1], list) and c.args[1][:2] == ["beet", "modify"]
    ]
    assert modify_calls == [
        ["beet", "modify", "-y", "id:1", "subbox_id=SBX-1"],
        ["beet", "modify", "-y", "id:2", "subbox_id=SBX-2"],
    ]


def test_remap_subbox_id_for_ids_skips_a_track_with_no_subbox_id_tag():
    beets_exec = BeetsExec()
    db_controller = mock.Mock()
    controller, _ = _make_controller(beets_exec, db_controller=db_controller)

    def fake_execute(container_name, command, stream=False):
        cmd = command if isinstance(command, list) else command.split()
        if cmd[:3] == ["beet", "list", "-f"]:
            return "1:/music/Artist/Album/one.mp3\n"
        return iter([])

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker, \
         mock.patch("pymix.controllers.rekordbox_xml_controller.get_subbox_id", return_value=None), \
         mock.patch.object(Path, "exists", return_value=True):
        mock_docker.execute.side_effect = fake_execute

        controller.remap_subbox_id_for_ids("demoadmin", [1], public=False)

    db_controller.add_subbox_beet_map.assert_not_called()
    modify_calls = [
        c for c in mock_docker.execute.call_args_list
        if isinstance(c.args[1], list) and c.args[1][:2] == ["beet", "modify"]
    ]
    assert modify_calls == []


def test_retag_duplicates_delegates_to_get_duplicates():
    beets_exec = BeetsExec()
    controller, _ = _make_controller(beets_exec)
    with mock.patch.object(controller, "_get_duplicates", return_value=["a", "b"]) as mock_get_duplicates:
        result = controller.retag_duplicates("demoadmin", public=False)

    mock_get_duplicates.assert_called_once_with("demoadmin", False)
    assert result == ["a", "b"]
