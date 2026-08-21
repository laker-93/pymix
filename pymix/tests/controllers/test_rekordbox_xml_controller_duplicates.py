"""
Resolving `beet duplicates` records to real files, across beets versions
(laker-93/pymix#65).

`beetsplug/duplicates.py` formats every record as `f"{fmt_tmpl}: {obj_count}"`:
unconditionally on 2.10.0, gated on the `--count` flag from 2.13.1 on. pymix never
passes `-c`, so the same library yields

    2.10.0    /music/Aphex Twin/SAW 85-92/01 - Aphex Twin - Xtal.1.flac: 1
    2.13.1    /music/Aphex Twin/SAW 85-92/01 - Aphex Twin - Xtal.1.flac

and the old shape is not a path that exists, so dup tagging silently did nothing for
every user on an old container. Asking for an explicit format does not help -- `-p`
and `-f` both set the same `format` config that becomes `fmt_tmpl`, so
`duplicates -f '$path'` on 2.10.0 is byte-identical, suffix included. The suffix has
to be stripped, and stripped defensively: only after the record fails to resolve as
written, so a real file whose name ends in `: 12` still wins.
"""
from pathlib import Path
from unittest import mock

import pytest

from pymix.clients.beets_exec import BeetsExec
from pymix.controllers.rekordbox_xml_controller import RekordboxXMLController


def _make_controller(beets_exec=None):
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
        beets_exec=beets_exec or BeetsExec(),
    )


def _only_these_exist(*existing):
    """Patch Path.exists so exactly the given absolute paths are on disk."""
    on_disk = set(existing)

    def fake_exists(self):
        return str(self) in on_disk

    return mock.patch.object(Path, "exists", fake_exists)


def _on_disk(record, username="alice"):
    """
    The pymix-side path a beets record names. beets sees the user's library mounted
    at /music; pymix sees the same files under /private-music/<user>.
    """
    return f"/private-music/{username}{record.removeprefix('/music')}"


# The record as each beets version emits it, and the file both name.
BARE = "/music/Aphex Twin/Selected Ambient Works 85–92/01 - Aphex Twin - Xtal.1.flac"
WITH_COUNT = f"{BARE}: 1"
ON_DISK = _on_disk(BARE)


@pytest.mark.parametrize(
    "record, label",
    [(BARE, "beets 2.13.1"), (WITH_COUNT, "beets 2.10.0")],
)
def test_both_beets_output_shapes_resolve_to_the_same_file(record, label):
    controller = _make_controller()
    with _only_these_exist(ON_DISK):
        resolved = controller._resolve_duplicate_path("alice", record)
    assert resolved == Path(ON_DISK), label


def test_a_file_whose_name_really_ends_in_a_count_wins_over_the_strip():
    """
    The strip must never mangle a genuine path. `…/track: 12.mp3: 12` is ambiguous
    between "2.10.0 emitted a count" and "the file is named that"; resolving the raw
    record first settles it in favour of the file that actually exists.
    """
    real = "/music/Artist/Album/weird: 12"
    controller = _make_controller()
    with _only_these_exist(_on_disk(real)):
        resolved = controller._resolve_duplicate_path("alice", real)
    assert resolved == Path(_on_disk(real))


def test_an_unresolvable_record_is_skipped_not_raised():
    controller = _make_controller()
    with _only_these_exist():
        assert controller._resolve_duplicate_path("alice", WITH_COUNT) is None


def test_only_a_trailing_integer_is_treated_as_a_count():
    """A colon inside the path is part of the name, not a count."""
    record = "/music/Artist/Album 2: The Sequel/track.mp3"
    controller = _make_controller()
    with _only_these_exist(_on_disk(record)):
        resolved = controller._resolve_duplicate_path("alice", record)
    assert resolved == Path(_on_disk(record))


def _tag(controller, records):
    with mock.patch("pymix.controllers.rekordbox_xml_controller.FooPlugin"), \
         mock.patch("pymix.controllers.rekordbox_xml_controller.Item") as mock_item, \
         _only_these_exist(ON_DISK):
        returned = controller._tag_duplicate_paths("alice", records)
    return mock_item, returned


def test_a_2_10_0_record_now_gets_tagged():
    """The regression itself: this used to log a warning and tag nothing."""
    mock_item, _ = _tag(_make_controller(), [WITH_COUNT])

    mock_item.from_path.assert_called_once_with(Path(ON_DISK))
    tagged = mock_item.from_path.return_value
    assert tagged.__setitem__.call_args.args == ("dup", "1")
    tagged.write.assert_called_once()


def test_tagged_paths_are_returned_without_the_count_suffix():
    """
    The return value reaches the caller via GET /beets/duplicates, so it should be
    the same clean path on every beets version -- not `…flac: 1`.
    """
    _, returned = _tag(_make_controller(), [WITH_COUNT])
    assert returned == [BARE]


def test_an_unresolvable_record_does_not_stop_the_rest():
    controller = _make_controller()
    mock_item, _ = _tag(controller, ["/music/gone.mp3: 3", WITH_COUNT])
    mock_item.from_path.assert_called_once_with(Path(ON_DISK))
