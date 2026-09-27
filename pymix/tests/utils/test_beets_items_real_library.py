"""
The dump and re-add scripts run against a real beets library (#209), in a subprocess
exactly as `docker exec` runs them in a user's beets container, but with BEETSDIR
pointing at a throwaway library.

The trash's unit tests fake beets. These are where the album handling is proven:
every beets import makes its own album, so one name can be several albums, and
beets reuses album ids.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

beets_library = pytest.importorskip('beets.library')

from pymix.utils.beets_items import build_add_command, build_dump_command, parse_json_lines

AUDIO = Path(__file__).resolve().parents[1] / 'fixtures' / 'audio' / 'tagged.mp3'


@pytest.fixture
def beetsdir(tmp_path):
    music = tmp_path / 'music'
    music.mkdir()
    (tmp_path / 'config.yaml').write_text(
        f"library: {tmp_path / 'library.db'}\ndirectory: {music}\nplugins: []\n"
    )
    return tmp_path


def _run(beetsdir, command):
    env = {**os.environ, 'BEETSDIR': str(beetsdir), 'HOME': str(beetsdir)}
    result = subprocess.run([sys.executable, *command[1:]], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return parse_json_lines(result.stdout)


def _library(beetsdir):
    return beets_library.Library(str(beetsdir / 'library.db'), str(beetsdir / 'music'))


def _import(beetsdir, albums):
    """albums: [(albumartist, album, [(subbox_id, relative path), ...]), ...], each one
    its own beets album, as each import makes one."""
    lib = _library(beetsdir)
    for albumartist, album_name, tracks in albums:
        items = []
        for subbox_id, relative in tracks:
            path = beetsdir / 'music' / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(AUDIO, path)
            item = beets_library.Item.from_path(str(path))
            item['subbox_id'] = subbox_id
            item['user'] = 'dj'
            item.title = subbox_id
            items.append(item)
        album = lib.add_album(items)
        # What an as-is import does and never writes back to the file.
        album.albumartist = albumartist
        album.album = album_name
        album['automatch_state'] = 'pending'
        album.store(inherit=True)
    lib._close()


def _state(beetsdir):
    """Every item's fields plus the album it is in, by subbox_id, and each file's sha."""
    lib = _library(beetsdir)
    state = {}
    for item in lib.items():
        album = item.get_album()
        state[item['subbox_id']] = {
            'fields': {k: item.get(k) for k in item.keys(computed=False) if k not in ('id', 'album_id')},
            'album': album and {k: album.get(k) for k in album.keys(computed=False) if k not in ('id', 'artpath')},
            'album_mates': sorted(i['subbox_id'] for i in album.items()) if album else [],
            'sha': hashlib.sha256(Path(os.fsdecode(item.path)).read_bytes()).hexdigest(),
        }
    lib._close()
    return state


def _delete(beetsdir, dumps):
    """What `beet rm -f` does: the rows go, the files stay, an emptied album goes."""
    lib = _library(beetsdir)
    for dump in dumps:
        for item in lib.items(f"subbox_id::^{dump['subbox_id']}$"):
            item.remove(delete=False, with_album=True)
    lib._close()


def _roundtrip(beetsdir, subbox_ids, order=None):
    dumps = _run(beetsdir, build_dump_command(subbox_ids))
    _delete(beetsdir, dumps)
    specs = [
        {'path': d['path'], 'fields': d['fields'], 'album': d['album']}
        for d in (sorted(dumps, key=lambda d: order.index(d['subbox_id'])) if order else dumps)
    ]
    return _run(beetsdir, build_add_command(specs))


def test_a_restored_item_is_as_it_was_and_its_file_untouched(beetsdir):
    _import(beetsdir, [('The Artist', 'The Album', [('a', 'A/a.mp3')])])
    before = _state(beetsdir)

    results = _roundtrip(beetsdir, ['a'])

    assert all('id' in r for r in results), results
    assert _state(beetsdir) == before


def test_a_track_rejoins_the_album_its_other_tracks_still_form(beetsdir):
    _import(beetsdir, [('The Artist', 'The Album', [('a', 'A/a.mp3'), ('b', 'A/b.mp3')])])
    before = _state(beetsdir)

    _roundtrip(beetsdir, ['a'])

    assert _state(beetsdir) == before
    assert _state(beetsdir)['a']['album_mates'] == ['a', 'b']


def test_same_named_albums_stay_separate(beetsdir):
    # Two imports of one album's tracks: two beets albums with one name.
    _import(beetsdir, [
        ('The Artist', 'The Album', [('a', 'A/a.mp3')]),
        ('The Artist', 'The Album', [('b', 'A/b.mp3')]),
    ])
    before = _state(beetsdir)

    _roundtrip(beetsdir, ['a', 'b'])

    after = _state(beetsdir)
    assert after == before
    assert after['a']['album_mates'] == ['a'] and after['b']['album_mates'] == ['b']


def test_a_recycled_album_id_is_not_mistaken_for_the_old_album(beetsdir):
    # Both albums go, so the table is empty. Recreating b's album first hands it id 1,
    # which was a's album's id, and the names match: a must not join it.
    _import(beetsdir, [
        ('The Artist', 'The Album', [('a', 'A/a.mp3')]),
        ('The Artist', 'The Album', [('b', 'A/b.mp3')]),
    ])
    before = _state(beetsdir)

    _roundtrip(beetsdir, ['a', 'b'], order=['b', 'a'])

    after = _state(beetsdir)
    assert after['a']['album_mates'] == ['a'] and after['b']['album_mates'] == ['b']
    assert after == before


def test_a_whole_album_comes_back_as_one(beetsdir):
    _import(beetsdir, [('The Artist', 'The Album', [('a', 'A/a.mp3'), ('b', 'A/b.mp3'), ('c', 'A/c.mp3')])])
    before = _state(beetsdir)

    _roundtrip(beetsdir, ['a', 'b', 'c'])

    assert _state(beetsdir) == before


def test_an_item_already_at_the_path_is_refused(beetsdir):
    _import(beetsdir, [('The Artist', 'The Album', [('a', 'A/a.mp3')])])
    dumps = _run(beetsdir, build_dump_command(['a']))

    results = _run(beetsdir, build_add_command([
        {'path': d['path'], 'fields': d['fields'], 'album': d['album']} for d in dumps
    ]))

    assert 'already has an item' in results[0]['error']
    assert len(_state(beetsdir)) == 1
