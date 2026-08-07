"""
The two post-import reads -- `beet duplicates -p` and `beet list -f $id:$path
subbox_id::^$` -- share one exec instead of one each. Each `beet` invocation pays
a full interpreter start plus the container's whole plugin chain (~0.35s measured
locally, 3-6s on prod per #100) to run a single query, so the second one is pure
overhead.

It has to degrade rather than break: the merged read goes through private beets
API (`ui._raw_main`, needed because `duplicates` is a plugin command with no
supported Python entry point) and per-user containers freeze their beets version
at provisioning -- the container-drift note in docs/dev.md.

Same harness as test_rekordbox_xml_controller_batched_writes.py: a real BeetsExec
with the underlying docker.execute patched.
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


MERGED_READ_OUTPUT = (
    "---PYMIX-DUPLICATES---\n"
    "---PYMIX-UNMAPPED---\n"
    "1:/music/Artist/Album/one.mp3\n"
    "2:/music/Artist/Album/two.mp3\n"
    "3:/music/Artist/Album/three.mp3\n"
    "---PYMIX-END---\n"
)


def _cmds(mock_docker):
    return [c.args[1] for c in mock_docker.execute.call_args_list]


def _merged_read_calls(mock_docker):
    return [
        c for c in _cmds(mock_docker)
        if isinstance(c, list) and c[:2] == ["python3", "-c"] and "---PYMIX-DUPLICATES---" in c
    ]


def _beet_calls(mock_docker, subcommand):
    return [
        c for c in _cmds(mock_docker)
        if (c if isinstance(c, list) else c.split())[:2] == ["beet", subcommand]
    ]


def _run_post_import(fake_execute, db_controller):
    beets_exec = BeetsExec()
    controller = _make_controller(beets_exec, db_controller=db_controller)
    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker, \
         mock.patch("pymix.controllers.rekordbox_xml_controller.get_subbox_id") as mock_get_subbox_id, \
         mock.patch.object(Path, "exists", return_value=True):
        mock_docker.execute.side_effect = fake_execute
        mock_get_subbox_id.side_effect = lambda p: f"SBX-{Path(p).stem}"
        controller._post_import_reads("alice", False)
        return mock_docker


def test_merged_read_replaces_both_beet_execs():
    def fake_execute(container_name, command, stream=False):
        cmd = command if isinstance(command, list) else command.split()
        if cmd[:2] == ["python3", "-c"]:
            if "---PYMIX-DUPLICATES---" in cmd:
                return MERGED_READ_OUTPUT
            return "APPLIED 3 MISSING 0"
        raise AssertionError(f"unexpected exec: {cmd}")

    db_controller = mock.Mock()
    mock_docker = _run_post_import(fake_execute, db_controller)

    assert len(_merged_read_calls(mock_docker)) == 1
    # The two reads it replaces must not also run.
    assert _beet_calls(mock_docker, "duplicates") == []
    assert _beet_calls(mock_docker, "list") == []
    # and every unmapped track still got mapped
    assert db_controller.add_subbox_beet_map.call_count == 3


def test_falls_back_to_the_separate_reads_when_the_merged_one_fails():
    # An old container without ui._raw_main: the exec raises rather than returning
    # parseable output, and the import must still map every track.
    def fake_execute(container_name, command, stream=False):
        cmd = command if isinstance(command, list) else command.split()
        if cmd[:2] == ["python3", "-c"]:
            if "---PYMIX-DUPLICATES---" in cmd:
                raise RuntimeError("AttributeError: module 'beets.ui' has no attribute '_raw_main'")
            return "APPLIED 3 MISSING 0"
        if cmd[:2] == ["beet", "duplicates"]:
            return ""
        if cmd[:2] == ["beet", "list"]:
            return iter([
                ("stdout", b"1:/music/Artist/Album/one.mp3"),
                ("stdout", b"2:/music/Artist/Album/two.mp3"),
                ("stdout", b"3:/music/Artist/Album/three.mp3"),
            ])
        raise AssertionError(f"unexpected exec: {cmd}")

    db_controller = mock.Mock()
    mock_docker = _run_post_import(fake_execute, db_controller)

    assert len(_beet_calls(mock_docker, "duplicates")) == 1
    assert len(_beet_calls(mock_docker, "list")) == 1
    assert db_controller.add_subbox_beet_map.call_count == 3


def test_unparseable_merged_output_falls_back_rather_than_importing_nothing():
    # The dangerous case: output that parses as "no duplicates, no unmapped items"
    # is indistinguishable from a healthy empty library, and silently skips the
    # subbox_id mapping for the whole import. Missing markers must force a fallback.
    def fake_execute(container_name, command, stream=False):
        cmd = command if isinstance(command, list) else command.split()
        if cmd[:2] == ["python3", "-c"]:
            if "---PYMIX-DUPLICATES---" in cmd:
                return "Traceback (most recent call last):\nImportError\n"
            return "APPLIED 3 MISSING 0"
        if cmd[:2] == ["beet", "duplicates"]:
            return ""
        if cmd[:2] == ["beet", "list"]:
            return iter([("stdout", b"1:/music/Artist/Album/one.mp3")])
        raise AssertionError(f"unexpected exec: {cmd}")

    db_controller = mock.Mock()
    mock_docker = _run_post_import(fake_execute, db_controller)

    assert len(_beet_calls(mock_docker, "list")) == 1
    assert db_controller.add_subbox_beet_map.call_count == 1


def test_duplicates_from_the_merged_read_are_tagged():
    merged = (
        "---PYMIX-DUPLICATES---\n"
        "/music/Artist/Album/dup.mp3\n"
        "---PYMIX-UNMAPPED---\n"
        "---PYMIX-END---\n"
    )

    def fake_execute(container_name, command, stream=False):
        cmd = command if isinstance(command, list) else command.split()
        if cmd[:2] == ["python3", "-c"] and "---PYMIX-DUPLICATES---" in cmd:
            return merged
        raise AssertionError(f"unexpected exec: {cmd}")

    beets_exec = BeetsExec()
    controller = _make_controller(beets_exec)
    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker, \
         mock.patch("pymix.controllers.rekordbox_xml_controller.FooPlugin"), \
         mock.patch("pymix.controllers.rekordbox_xml_controller.Item") as mock_item, \
         mock.patch.object(Path, "exists", return_value=True):
        mock_docker.execute.side_effect = fake_execute
        controller._post_import_reads("alice", False)

    mock_item.from_path.assert_called_once()
    tagged = mock_item.from_path.return_value
    assert tagged.__setitem__.call_args.args == ("dup", "1")
    tagged.write.assert_called_once()
