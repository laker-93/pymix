"""
One subbox_id, one path -- even when beets holds two items for it.

The per-user beets config sets `duplicate_action: keep`, and a subbox_id lives in
the file's own tags, so re-uploading a file beets already has leaves two items
carrying the same id. That is the ordinary aftermath of an import that failed
*after* `beet import` landed the audio, because the user's next move is to retry
the upload.

`beet ls -p` then prints two lines, and `get_path_by_subbox_id` used to hand back
`Path("<first>\n<second>")`. Nothing downstream inspects the path -- it just fails
`exists()` -- so every crate entry was dropped as "no file in your library for that
track" and the Serato import died with "none of the N tracks in your N crates are
in your subbox library", which points at the crates instead of at the duplicate.
"""
from pathlib import Path
from unittest import mock

from pymix.controllers.rekordbox_xml_controller import RekordboxXMLController


ORIGINAL = "/music/Subbox QA/Grid Fixtures/00 - Variable Tempo.mp3"
RETRY_DUPLICATE = "/music/Subbox QA/Grid Fixtures/00 - Variable Tempo.1.mp3"
SUBBOX_ID = "6874928d-2872-4bdb-9f2b-57a59878bd57"


def _make_controller(ls_output):
    beets_exec = mock.Mock()
    beets_exec.execute.return_value = ls_output
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
        serving_music_path_base="/private-music",
        beets_exec=beets_exec,
    )


def test_the_single_item_case_is_unchanged():
    controller = _make_controller(f"{ORIGINAL}\n")
    assert controller.get_path_by_subbox_id("alice", SUBBOX_ID, False) == Path(ORIGINAL)


def test_a_duplicate_resolves_to_the_original_not_a_two_line_path():
    """The regression: the returned path used to contain a newline and exist nowhere."""
    controller = _make_controller(f"{ORIGINAL}\n{RETRY_DUPLICATE}\n")

    path = controller.get_path_by_subbox_id("alice", SUBBOX_ID, False)

    assert path == Path(ORIGINAL)
    assert "\n" not in str(path)


def test_no_match_yields_no_path_components():
    """
    Nothing found stays the falsy-ish `Path('.')` the caller already handled, rather
    than becoming a new exception on a path that was previously merely useless.
    """
    controller = _make_controller("")
    assert controller.get_path_by_subbox_id("alice", SUBBOX_ID, False) == Path(".")


def test_the_public_library_uses_the_shared_beets_container():
    controller = _make_controller(f"{ORIGINAL}\n")
    controller.get_path_by_subbox_id("alice", SUBBOX_ID, True)
    assert controller._beets_exec.execute.call_args.args[0] == "beets"
