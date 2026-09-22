"""
What the batched write actually puts in the audio file (laker-93/pymix#180).

These run the real `_SET_FIELD_SCRIPT` against a real beets library holding a
real MP3, because the defect was invisible from the argv side: the command it
builds is unchanged, and the damage only appears in the file the script writes.

The shape under test is the one a Rekordbox import leaves behind -- a beets row
that disagrees with the file on a field nobody asked to touch. `item.try_write()`
flushed the whole row, so setting `bpm` also pushed the stale row value over the
file's. lastgenre (#179) produced exactly that divergence on every import.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from beets.library import Item, Library
from mediafile import MediaFile

from pymix.utils.beets_batch import (
    MATCH_BY_ID,
    build_set_field_command,
    parse_applied,
    parse_missing,
    parse_write_failures,
)

FIXTURE_MP3 = Path(__file__).resolve().parents[1] / "fixtures" / "audio" / "tagged.mp3"


class _Beets:
    """A throwaway beets library on disk, plus the one track in it."""

    def __init__(self, root: Path, track: Path, db: Path, item_id: int):
        self.root = root
        self.track = track
        self.db = db
        self.item_id = item_id

    def run(self, field, value, write_tags, match_field=MATCH_BY_ID, key=None):
        command = build_set_field_command(
            field, match_field, [(self.item_id if key is None else key, value)], write_tags
        )
        # The container runs `python3`; here it has to be the interpreter that
        # actually has beets and mediafile installed.
        env = dict(os.environ, BEETSDIR=str(self.root), HOME=str(self.root))
        return subprocess.run(
            [sys.executable, *command[1:]],
            capture_output=True, text=True, env=env,
        )

    def row(self):
        lib = Library(str(self.db), str(self.track.parent))
        try:
            return lib.get_item(self.item_id)
        finally:
            lib._close()

    def tags(self):
        return MediaFile(str(self.track))


@pytest.fixture
def beets(tmp_path):
    """
    A one-track library whose beets row disagrees with the file: the DB genre is
    empty and the file's is not. That is the state #179's lastgenre left behind,
    and the state in which a bpm-only write destroyed the file's genre.
    """
    music = tmp_path / "music"
    music.mkdir()
    track = music / "track.mp3"
    shutil.copy(FIXTURE_MP3, track)

    db = tmp_path / "library.db"
    (tmp_path / "config.yaml").write_text(
        f"directory: {music}\nlibrary: {db}\nplugins: []\n"
    )

    lib = Library(str(db), str(music))
    item = Item.from_path(str(track))
    with lib.transaction():
        lib.add(item)
    item_id = item.id
    # The divergence, DB side only -- the file keeps its genre.
    item.genre = ""
    item.title = "Row Title The File Does Not Have"
    item.store()
    lib._close()

    return _Beets(tmp_path, track, db, item_id)


def test_the_fixture_starts_diverged(beets):
    # Guards the guard: if the fixture ever stopped disagreeing with the file,
    # every assertion below would pass for the wrong reason.
    assert beets.tags().genre == "Techno"
    assert beets.tags().title == "Test Track"
    assert beets.row().genre == ""
    assert beets.row().title == "Row Title The File Does Not Have"


def test_a_bpm_write_leaves_every_other_tag_in_the_file_alone(beets):
    result = beets.run("bpm", 120, write_tags=True)

    assert parse_applied(result.stdout) == 1, result.stderr
    tags = beets.tags()
    assert tags.bpm == 120
    # The whole point: neither of these was asked for, so neither may change.
    # Before #180 the genre came back None and the title came back the row's.
    assert tags.genre == "Techno"
    assert tags.title == "Test Track"
    assert tags.artist == "Test Artist"
    assert tags.album == "Test Album"


def test_the_bpm_still_lands_in_the_row_as_well_as_the_file(beets):
    beets.run("bpm", 120, write_tags=True)

    assert beets.row().bpm == 120


def test_the_row_mtime_moves_with_the_file(beets):
    # Item.write() does this, and a scoped write has to as well: a row whose
    # mtime predates its file reads to beets as edited outside beets, which is
    # the condition `beet update` acts on.
    before = beets.row().mtime

    beets.run("bpm", 120, write_tags=True)

    assert beets.row().mtime >= before
    assert beets.row().mtime == int(beets.track.stat().st_mtime)


def test_nowrite_mode_does_not_touch_the_file_at_all(beets):
    before = beets.track.read_bytes()

    result = beets.run("subbox_id", "SBX-1", write_tags=False)

    assert parse_applied(result.stdout) == 1, result.stderr
    assert beets.row().subbox_id == "SBX-1"
    assert beets.track.read_bytes() == before


def test_an_unwritable_file_is_reported_rather_than_swallowed(beets):
    # The row is updated and the file is not, so the two now disagree on bpm.
    # try_write() logged this to a beets logger nothing reads, which is how an
    # import whose every write failed still reported success (#135).
    beets.track.unlink()

    result = beets.run("bpm", 120, write_tags=True)

    assert parse_applied(result.stdout) == 1, result.stderr
    failures = parse_write_failures(result.stdout)
    assert [key for key, _ in failures] == [str(beets.item_id)]
    assert failures[0][1]
    assert beets.row().bpm == 120


def test_a_missing_key_is_still_reported_as_missing(beets):
    result = beets.run("bpm", 120, write_tags=True, key=beets.item_id + 999)

    assert parse_applied(result.stdout) == 0, result.stderr
    assert parse_missing(result.stdout) == [str(beets.item_id + 999)]
    assert parse_write_failures(result.stdout) == []


def test_asking_to_write_a_field_no_file_can_hold_reports_no_summary(beets):
    # subbox_id is a flexattr: MediaFile has nowhere to put it. Rather than
    # update rows and claim a tag write happened, the script declines to print
    # its summary, which is what makes the caller fall back.
    result = beets.run("subbox_id", "SBX-1", write_tags=True)

    assert result.returncode != 0
    assert "NOTAGFIELD subbox_id" in result.stderr
    with pytest.raises(ValueError):
        parse_applied(result.stdout)
