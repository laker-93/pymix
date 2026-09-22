"""
The genre heal (laker-93/pymix#179) driven for real against a live beets library.

The rest of the heal's tests check the argv and parse the report. These run the
embedded script itself -- real beets, real library DB, real tagged files -- because
the only thing that matters about it is what it does to a user's rows, and a string
assertion cannot see that. Skipped where beets isn't importable.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from pymix.utils.beets_batch import build_heal_genre_command, parse_heal_genre

beets = pytest.importorskip("beets")
mediafile = pytest.importorskip("mediafile")

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "audio" / "tagged.mp3"


def _write_genre(path, genre):
    media = mediafile.MediaFile(str(path))
    media.genre = genre
    media.save()


@pytest.fixture
def library(tmp_path):
    """
    A beets library in the state lastgenre left a real user's: the file still holds
    the genre the DJ set, the DB row is empty.
    """
    from beets.library import Item, Library

    music = tmp_path / "music"
    music.mkdir()

    # The damage: file genre set, DB genre emptied.
    damaged = music / "damaged.mp3"
    shutil.copy(FIXTURE, damaged)
    _write_genre(damaged, "BASS HOUSE")

    # Never had a genre to begin with -- nothing to restore from.
    no_source = music / "nosource.mp3"
    shutil.copy(FIXTURE, no_source)
    _write_genre(no_source, "")

    # Untouched: both sides agree, and the heal must leave it exactly alone.
    healthy = music / "healthy.mp3"
    shutil.copy(FIXTURE, healthy)
    _write_genre(healthy, "Techno")

    # A row whose file is gone. `beet update` would DELETE this row; the heal
    # must report it and leave it.
    orphan = music / "orphan.mp3"
    shutil.copy(FIXTURE, orphan)

    lib_path = tmp_path / "musiclibrary.blb"
    lib = Library(str(lib_path), str(music))
    for path in (damaged, no_source, healthy, orphan):
        item = Item.from_path(str(path))
        item.add(lib)
    # lastgenre's effect: empty the DB genre, leave the file alone.
    for item in lib.items():
        if Path(item.path.decode()).name in ("damaged.mp3", "nosource.mp3", "orphan.mp3"):
            item["genre"] = ""
            item.store()
    lib._close()

    orphan.unlink()

    beetsdir = tmp_path / "beetsdir"
    beetsdir.mkdir()
    (beetsdir / "config.yaml").write_text(
        f"library: {lib_path}\ndirectory: {music}\nplugins: []\n"
    )
    return {"beetsdir": beetsdir, "lib_path": lib_path, "music": music}


def _run(library, apply_changes):
    command = build_heal_genre_command(apply_changes)
    env = {**os.environ, "BEETSDIR": str(library["beetsdir"])}
    proc = subprocess.run(
        [sys.executable, *command[1:]], capture_output=True, text=True, env=env
    )
    assert proc.returncode == 0, proc.stderr
    return parse_heal_genre(proc.stdout)


def _db_genres(library):
    from beets.library import Library

    lib = Library(str(library["lib_path"]), str(library["music"]))
    genres = {Path(i.path.decode()).name: i.genre for i in lib.items()}
    lib._close()
    return genres


def test_a_dry_run_reports_the_damage_and_changes_nothing(library):
    before = _db_genres(library)

    result = _run(library, apply_changes=False)

    assert result.applied is False
    assert result.healed == 1
    assert [genre for genre, _ in result.changes] == ["BASS HOUSE"]
    assert [Path(p).name for _, p in result.changes] == ["damaged.mp3"]
    # and the library is untouched
    assert _db_genres(library) == before
    assert before["damaged.mp3"] == ""


def test_applying_restores_the_genre_from_the_file(library):
    result = _run(library, apply_changes=True)

    assert result.applied is True
    assert result.healed == 1
    assert _db_genres(library)["damaged.mp3"] == "BASS HOUSE"


def test_it_leaves_a_row_that_already_has_a_genre_alone(library):
    _run(library, apply_changes=True)

    assert _db_genres(library)["healthy.mp3"] == "Techno"


def test_a_row_with_no_genre_in_the_file_is_reported_not_invented(library):
    result = _run(library, apply_changes=True)

    assert [Path(p).name for p in result.no_source] == ["nosource.mp3"]
    assert _db_genres(library)["nosource.mp3"] == ""


def test_a_row_whose_file_is_missing_is_reported_and_kept(library):
    # This is the whole reason the heal is not `beet update -M -F genre`: update
    # removes items whose files are missing, so the repair would delete rows.
    result = _run(library, apply_changes=True)

    assert [Path(p).name for p in result.unreadable] == ["orphan.mp3"]
    assert "orphan.mp3" in _db_genres(library)


def test_it_never_rewrites_the_audio_file(library):
    # #180: flushing the beets row back into the file is what put the emptied
    # genre on disk. The heal reads the file and must never write it.
    music = library["music"]
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in music.glob("*.mp3")}

    _run(library, apply_changes=True)

    after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in music.glob("*.mp3")}
    assert after == before


def test_it_is_safe_to_run_twice(library):
    _run(library, apply_changes=True)
    second = _run(library, apply_changes=True)

    assert second.healed == 0
    assert _db_genres(library)["damaged.mp3"] == "BASS HOUSE"
