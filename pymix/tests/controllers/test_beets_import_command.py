"""
Direct-construction tests (no DI container) for the beet import command built
at each of the three call sites listed in laker-93/pymix#74. Avoids the
pre-existing /app template-path limitation that DI-container-based tests hit
outside the Docker image (see pymix/tests/fixtures/container.py).

Uses a real BeetsExec (so write_lock actually works as a context manager) with
the underlying docker.execute call patched out, same pattern as
test_beets_exec.py.
"""
from unittest import mock

from pymix.clients.beets_exec import BeetsExec
from pymix.controllers.rekordbox_xml_controller import RekordboxXMLController
from pymix.controllers.serato_controller import SeratoController


def _make_rekordbox_xml_controller(beets_exec):
    return RekordboxXMLController(
        subsonic_orchestrator=mock.Mock(),
        rekordbox_xml_orchestrator=mock.Mock(),
        rb_backup_file_handler=mock.Mock(),
        file_browser_file_handler=mock.Mock(),
        subsonic_client=mock.Mock(),
        db_controller=mock.Mock(),
        wishlist_reconcile_service=mock.Mock(),
        restored_db_output_root="foo",
        local_user_music_stem="foo",
        serving_music_path_base="foo",
        beets_exec=beets_exec,
    )


def test_consume_from_filebrowser_imports_as_is_and_stamps_automatch_pending():
    beets_exec = BeetsExec()
    controller = _make_rekordbox_xml_controller(beets_exec)

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker:
        # empty result/iterables for the subsequent duplicates + subbox_id-map
        # reads: this job's finally block always runs, so _get_duplicates /
        # _map_subbox_id_beet_id call execute() again after the import call.
        mock_docker.execute.return_value = []
        controller._consume_from_filebrowser("demoadmin", public=False, watch=False)

    container_name, command = mock_docker.execute.call_args_list[0].args
    assert container_name == "beetsdemoadmin"
    assert command == [
        "beet", "import", "-A", "--group-albums", "--set", "user=demoadmin",
        "--set", "public=False", "--set", "automatch_state=pending", "/downloads",
    ]
    assert "-q" not in command


def test_rekordbox_import_to_beets_imports_as_is_and_stamps_automatch_pending():
    beets_exec = BeetsExec()
    controller = _make_rekordbox_xml_controller(beets_exec)

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker:
        mock_docker.execute.return_value = []
        controller._import_to_beets("demoadmin", zip_path=None, audio_path=None, rekordbox_xml=mock.Mock())

    container_name, command = mock_docker.execute.call_args_list[0].args
    assert container_name == "beetsdemoadmin"
    assert command == [
        "beet", "import", "-A", "--group-albums", "--set", "user=demoadmin",
        "--set", "automatch_state=pending", "/downloads",
    ]
    assert "-q" not in command


def test_serato_import_to_beets_imports_as_is_and_stamps_automatch_pending():
    beets_exec = BeetsExec()
    mock_rb_xml_controller = mock.Mock()

    controller = SeratoController(
        subsonic_orchestrator=mock.Mock(),
        serato_crate_orchestrator=mock.Mock(),
        serato_backup_file_handler=mock.Mock(),
        file_browser_file_handler=mock.Mock(),
        rb_backup_file_handler=mock.Mock(),
        rb_xml_controller=mock_rb_xml_controller,
        db_controller=mock.Mock(),
        wishlist_reconcile_service=mock.Mock(),
        serving_music_path_base="foo",
        beets_exec=beets_exec,
    )

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker:
        mock_docker.execute.return_value = []
        controller._import_to_beets("demoadmin", zip_path=None, audio_path=None)

    container_name, command = mock_docker.execute.call_args_list[0].args
    assert container_name == "beetsdemoadmin"
    assert command == [
        "beet", "import", "-A", "--group-albums", "--set", "user=demoadmin",
        "--set", "automatch_state=pending", "/downloads",
    ]
    assert "-q" not in command
