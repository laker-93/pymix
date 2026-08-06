"""
The two post-import passes must not shell into the beets container once per
track (laker-93/pymix#51): at 3-6s per exec a 100-track import spent ~13 minutes
in the tail. Both passes now collect their writes and apply them in one batched
exec, falling back to the old per-track `beet modify` loop if that batch can't
run -- a per-user container can be on an older beets than the current template
(the container-drift note in docs/dev.md), so this has to degrade, not break.

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
    )


def _batch_calls(mock_docker):
    return [
        c.args[1] for c in mock_docker.execute.call_args_list
        if isinstance(c.args[1], list) and c.args[1][:2] == ["python3", "-c"]
    ]


def _modify_calls(mock_docker):
    return [
        c.args[1] for c in mock_docker.execute.call_args_list
        if isinstance(c.args[1], list) and c.args[1][:2] == ["beet", "modify"]
    ]


def _fake_list_then(batch_result):
    """beet list output for three imported tracks, then `batch_result` for the
    batched write (a str to succeed, an Exception to fail)."""
    def fake_execute(container_name, command, stream=False):
        cmd = command if isinstance(command, list) else command.split()
        if cmd[:2] == ["beet", "list"]:
            return iter([
                ("stdout", b"1:/music/Artist/Album/one.mp3"),
                ("stdout", b"2:/music/Artist/Album/two.mp3"),
                ("stdout", b"3:/music/Artist/Album/three.mp3"),
            ])
        if cmd[:2] == ["python3", "-c"]:
            if isinstance(batch_result, Exception):
                raise batch_result
            return batch_result
        return iter([])
    return fake_execute


def test_map_subbox_id_uses_one_batched_exec_for_every_track():
    beets_exec = BeetsExec()
    db_controller = mock.Mock()
    controller = _make_controller(beets_exec, db_controller=db_controller)

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker, \
         mock.patch("pymix.controllers.rekordbox_xml_controller.get_subbox_id") as mock_get_subbox_id, \
         mock.patch.object(Path, "exists", return_value=True):
        mock_docker.execute.side_effect = _fake_list_then("APPLIED 3 MISSING 0\n")
        mock_get_subbox_id.side_effect = ["SBX-1", "SBX-2", "SBX-3"]

        controller._map_subbox_id_beet_id("demoadmin", public=False)

    # One exec for all three tracks, not one per track...
    batches = _batch_calls(mock_docker)
    assert len(batches) == 1
    assert batches[0][3:] == ["subbox_id", "id", "nowrite", "1=SBX-1", "2=SBX-2", "3=SBX-3"]
    # ...and no per-track `beet modify` at all.
    assert _modify_calls(mock_docker) == []
    # The DB mapping is unchanged by the batching.
    assert db_controller.add_subbox_beet_map.call_args_list == [
        mock.call(username="demoadmin", subbox_id="SBX-1", beet_id=1),
        mock.call(username="demoadmin", subbox_id="SBX-2", beet_id=2),
        mock.call(username="demoadmin", subbox_id="SBX-3", beet_id=3),
    ]


def test_map_subbox_id_falls_back_to_per_track_modify_when_the_batch_fails():
    beets_exec = BeetsExec()
    controller = _make_controller(beets_exec)

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker, \
         mock.patch("pymix.controllers.rekordbox_xml_controller.get_subbox_id") as mock_get_subbox_id, \
         mock.patch.object(Path, "exists", return_value=True):
        mock_docker.execute.side_effect = _fake_list_then(RuntimeError("no python3 in container"))
        mock_get_subbox_id.side_effect = ["SBX-1", "SBX-2", "SBX-3"]

        controller._map_subbox_id_beet_id("demoadmin", public=False)

    # -M (never move): a rename mid-modify is what loses Navidrome identity, #94.
    assert _modify_calls(mock_docker) == [
        ["beet", "modify", "-y", "-M", "id:1", "subbox_id=SBX-1"],
        ["beet", "modify", "-y", "-M", "id:2", "subbox_id=SBX-2"],
        ["beet", "modify", "-y", "-M", "id:3", "subbox_id=SBX-3"],
    ]


def test_map_subbox_id_falls_back_when_the_batch_output_has_no_summary():
    # Exit code 0 but no APPLIED line: the exec ran something that wasn't our
    # script to completion, so the writes cannot be assumed to have landed.
    beets_exec = BeetsExec()
    controller = _make_controller(beets_exec)

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker, \
         mock.patch("pymix.controllers.rekordbox_xml_controller.get_subbox_id") as mock_get_subbox_id, \
         mock.patch.object(Path, "exists", return_value=True):
        mock_docker.execute.side_effect = _fake_list_then("")
        mock_get_subbox_id.side_effect = ["SBX-1", "SBX-2", "SBX-3"]

        controller._map_subbox_id_beet_id("demoadmin", public=False)

    assert len(_modify_calls(mock_docker)) == 3


def test_bpm_writes_go_out_as_one_batch_with_tag_write_on():
    beets_exec = BeetsExec()
    controller = _make_controller(beets_exec)

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker:
        mock_docker.execute.return_value = "APPLIED 2 MISSING 0\n"
        controller._modify_bpms("demoadmin", [("SBX-1", 128), ("SBX-2", 174)])

    batches = _batch_calls(mock_docker)
    assert len(batches) == 1
    # bpm is a real media field, so the value is written back to the file too --
    # matching what `beet modify` did before.
    assert batches[0][3:] == ["bpm", "subbox_id", "write", "SBX-1=128", "SBX-2=174"]
    assert _modify_calls(mock_docker) == []


def test_bpm_writes_fall_back_to_per_track_modify_when_the_batch_fails():
    beets_exec = BeetsExec()
    controller = _make_controller(beets_exec)

    def fake_execute(container_name, command, stream=False):
        cmd = command if isinstance(command, list) else command.split()
        if cmd[:2] == ["python3", "-c"]:
            raise RuntimeError("batch unsupported")
        return iter([])

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker:
        mock_docker.execute.side_effect = fake_execute
        controller._modify_bpms("demoadmin", [("SBX-1", 128), ("SBX-2", 174)])

    assert _modify_calls(mock_docker) == [
        ["beet", "modify", "-y", "subbox_id:SBX-1", "bpm=128"],
        ["beet", "modify", "-y", "subbox_id:SBX-2", "bpm=174"],
    ]


def test_bpm_writes_are_a_noop_with_nothing_to_write():
    beets_exec = BeetsExec()
    controller = _make_controller(beets_exec)

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker:
        controller._modify_bpms("demoadmin", [])

    mock_docker.execute.assert_not_called()
