"""
The trash (#200): DELETE /track moves a track's file aside instead of unlinking it,
and the reaper destroys it once it expires.

beets and Navidrome are faked; the database (SQLite) and the filesystem (tmp_path)
are real, because what matters here is where files end up and what the quota
counter says about them.
"""
import datetime
import hashlib
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from pymix.controllers.db_controller import DbController
from pymix.model.db_tables import Base, LibraryRow, SubboxBeetsMapRow, UserRow
from pymix.services.trash import ItemState, TrashService, batch_state

MAX_BYTES = 10_000
RETENTION_S = 7 * 24 * 3600


class FakeBeets:
    """A beets library of (subbox_id, path) items, answering the two commands the
    trash sends: `beet list -f '$subbox_id|$path' <query>` and `beet rm -f <query>`."""

    def __init__(self):
        self.items: list[tuple[str, str]] = []
        self.fail_rm = False
        self.commands: list[list[str]] = []
        self._lock = threading.Lock()

    @contextmanager
    def write_lock(self, container_name):
        with self._lock:
            yield

    def add(self, subbox_id: str, relative: str):
        self.items.append((subbox_id, f'/music/{relative}'))

    @staticmethod
    def _ids(command):
        return {token.split('::', 1)[1] for token in command if token.startswith('subbox_id::')}

    def execute(self, container_name, command):
        self.commands.append(list(command))
        ids = self._ids(command)
        if command[:2] == ['beet', 'list']:
            return '\n'.join(f'{i}|{p}' for i, p in self.items if i in ids)
        if command[:3] == ['beet', 'rm', '-f']:
            if self.fail_rm:
                raise RuntimeError('beets exploded')
            self.items = [(i, p) for i, p in self.items if i not in ids]
            return ''
        raise AssertionError(f'unexpected beets command {command}')


class FakeNative:
    """Navidrome's native API: a media_file table keyed by id."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.fail_lookup = False
        self.undeletable: set[str] = set()
        self.deleted: list[str] = []

    def add(self, media_id, relative, subbox_id, missing=False, updated_at='2026-09-26T10:00:00.5Z'):
        self.rows[media_id] = {
            'id': media_id, 'path': relative, 'missing': missing,
            'updatedAt': updated_at, 'tags': {'subboxid': [subbox_id]},
        }

    def go_missing(self, relative, updated_at='2026-09-26T10:00:00.5Z'):
        for row in self.rows.values():
            if row['path'] == relative:
                row['missing'] = True
                row['updatedAt'] = updated_at

    async def songs_by_subbox_id(self, user, subbox_ids):
        if self.fail_lookup:
            raise RuntimeError('navidrome is down')
        ids = set(subbox_ids)
        return [r for r in self.rows.values() if ids & set(r['tags']['subboxid'])]

    async def list_missing(self, user):
        return [r for r in self.rows.values() if r['missing']]

    async def delete_missing(self, user, media_file_ids):
        for media_id in media_file_ids:
            self.deleted.append(media_id)
            if media_id in self.rows and self.rows[media_id]['missing'] and media_id not in self.undeletable:
                del self.rows[media_id]


@pytest.fixture
def library_base(tmp_path):
    base = tmp_path / 'private-music'
    (base / 'dj').mkdir(parents=True)
    return base


@pytest.fixture
def db_controller(library_base, tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    controller = DbController(
        session_factory=sessionmaker(bind=engine),
        app_env="test",
        max_library_size=MAX_BYTES,
        serving_music_path_base=str(library_base),
        staging_path=f'{tmp_path}/private-staged/{{user}}/',
    )
    with controller._session_factory() as session:
        session.add(UserRow(
            username="dj", password="pw", email="dj@example.com", user_id="user-1",
            beets_port=1, subsonic_port=2, max_library_size=MAX_BYTES,
        ))
        session.commit()
    return controller


@pytest.fixture
def beets():
    return FakeBeets()


@pytest.fixture
def native():
    return FakeNative()


@pytest.fixture
def trash(db_controller, beets, native):
    return TrashService(db_controller, beets, native, retention_s=RETENTION_S)


def _track(db_controller, beets, native, library_base, subbox_id, relative, size=100, media_id=None):
    path = library_base / 'dj' / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'x' * size)
    beets.add(subbox_id, relative)
    with db_controller._session_factory() as session:
        session.add(SubboxBeetsMapRow(user_id='user-1', subbox_id=subbox_id, beet_id=len(beets.items)))
        session.add(LibraryRow(user_id='user-1', subbox_id=subbox_id, cuedata={'cues': [1]}, source_app='rb', version=1))
        session.commit()
    if media_id:
        native.add(media_id, relative, subbox_id)
    return path


def _measure(db_controller):
    # Start the counter from a walk, as the first quota check would.
    return db_controller.reconcile_library_bytes('dj')


# --- delete -----------------------------------------------------------------------

@pytest.mark.anyio
async def test_a_delete_moves_the_file_to_the_trash_at_the_same_relative_path(
        trash, db_controller, beets, native, library_base):
    source = _track(db_controller, beets, native, library_base, 'a', 'Artist/Album/01 a.mp3', media_id='mf-a')

    outcome = await trash.trash_tracks('dj', ['a'])

    assert outcome.removed == {'a'} and not outcome.not_removed
    target = library_base / '_trash' / 'dj' / outcome.batch_id / 'Artist/Album/01 a.mp3'
    assert target.read_bytes() == b'x' * 100
    assert not source.exists()
    # beets lost the item; `-f` and never `-d`, because the file has already gone.
    assert beets.items == []
    assert ['beet', 'rm', '-f', 'subbox_id::a'] in beets.commands
    # What `-d` used to prune.
    assert not (library_base / 'dj' / 'Artist').exists()
    assert (library_base / 'dj').is_dir()


@pytest.mark.anyio
async def test_the_batch_holds_everything_a_restore_needs(trash, db_controller, beets, native, library_base):
    _track(db_controller, beets, native, library_base, 'a', 'A/a.mp3', size=123, media_id='mf-a')

    outcome = await trash.trash_tracks('dj', ['a'])

    batch = db_controller.get_trash_batch(outcome.batch_id, 'dj')
    assert batch['kind'] == 'track' and batch['label'] == '1 track' and batch['bytes'] == 123
    assert batch['expires_at'] - batch['created_at'] == pytest.approx(RETENTION_S)
    [item] = batch['items']
    assert item['state'] == ItemState.RESTORABLE.value
    assert item['relative_path'] == 'A/a.mp3' and item['size'] == 123
    assert item['sha256'] == hashlib.sha256(b'x' * 123).hexdigest()
    assert item['media_file_id'] == 'mf-a'
    assert item['snapshot']['subbox_beets_map']['beet_id'] == 1
    assert item['snapshot']['library']['cuedata'] == {'cues': [1]}
    assert item['snapshot']['original_track_meta'] is None


@pytest.mark.anyio
async def test_a_delete_into_the_trash_leaves_the_quota_unchanged(trash, db_controller, beets, native, library_base):
    _track(db_controller, beets, native, library_base, 'a', 'A/a.mp3', size=300)
    assert _measure(db_controller) == 300

    await trash.trash_tracks('dj', ['a'])

    # Nothing was freed: the bytes are still on disk.
    assert db_controller.library_bytes('dj') == 300
    assert db_controller.trash_bytes('dj') == 300
    # And the daily reconcile agrees, because it walks the trash too.
    assert db_controller.reconcile_library_bytes('dj') == 300


@pytest.mark.anyio
async def test_ids_beets_does_not_have_are_already_deleted(trash, db_controller, beets, native, library_base):
    outcome = await trash.trash_tracks('dj', ['ghost'])

    assert outcome.removed == {'ghost'}
    assert outcome.batch_id is None
    assert db_controller.get_trash_batches('dj') == []


@pytest.mark.anyio
async def test_a_duplicate_upload_trashes_both_files(trash, db_controller, beets, native, library_base):
    _track(db_controller, beets, native, library_base, 'a', 'A/a.mp3')
    (library_base / 'dj' / 'A/a.1.mp3').write_bytes(b'y' * 50)
    beets.add('a', 'A/a.1.mp3')

    outcome = await trash.trash_tracks('dj', ['a'])

    batch = db_controller.get_trash_batch(outcome.batch_id)
    assert sorted(i['relative_path'] for i in batch['items']) == ['A/a.1.mp3', 'A/a.mp3']
    assert batch['bytes'] == 150
    assert not (library_base / 'dj' / 'A').exists()


@pytest.mark.anyio
async def test_a_failed_rm_puts_the_file_back_and_keeps_nothing_in_the_trash(
        trash, db_controller, beets, native, library_base):
    source = _track(db_controller, beets, native, library_base, 'a', 'A/a.mp3')
    beets.fail_rm = True

    outcome = await trash.trash_tracks('dj', ['a'])

    assert 'a' in outcome.not_removed and not outcome.removed
    assert source.read_bytes() == b'x' * 100
    assert beets.items == [('a', '/music/A/a.mp3')]
    assert outcome.batch_id is None
    assert db_controller.get_trash_batches('dj') == []
    assert not any((library_base / '_trash' / 'dj').rglob('*.mp3'))


@pytest.mark.anyio
async def test_a_failed_move_leaves_beets_alone(trash, db_controller, beets, native, library_base, monkeypatch):
    source = _track(db_controller, beets, native, library_base, 'a', 'A/a.mp3')
    _track(db_controller, beets, native, library_base, 'b', 'B/b.mp3')
    import pymix.services.trash as trash_module
    real_rename = trash_module.os.rename

    def rename(src, dst):
        if Path(src) == source:
            raise OSError(18, 'Invalid cross-device link')
        return real_rename(src, dst)
    monkeypatch.setattr(trash_module.os, 'rename', rename)

    outcome = await trash.trash_tracks('dj', ['a', 'b'])

    assert 'a' in outcome.not_removed and outcome.removed == {'b'}
    assert source.is_file()
    assert beets.items == [('a', '/music/A/a.mp3')]
    [item] = db_controller.get_trash_batch(outcome.batch_id)['items']
    assert item['subbox_id'] == 'b'


@pytest.mark.anyio
async def test_a_snapshot_that_fails_touches_nothing(trash, db_controller, beets, native, library_base, monkeypatch):
    source = _track(db_controller, beets, native, library_base, 'a', 'A/a.mp3')
    monkeypatch.setattr(db_controller, 'snapshot_track_rows', lambda *a: (_ for _ in ()).throw(RuntimeError('db down')))

    with pytest.raises(RuntimeError):
        await trash.trash_tracks('dj', ['a'])

    assert source.is_file() and beets.items


@pytest.mark.anyio
async def test_navidrome_being_down_does_not_block_a_delete(trash, db_controller, beets, native, library_base):
    _track(db_controller, beets, native, library_base, 'a', 'A/a.mp3', media_id='mf-a')
    native.fail_lookup = True

    outcome = await trash.trash_tracks('dj', ['a'])

    [item] = db_controller.get_trash_batch(outcome.batch_id)['items']
    assert item['state'] == ItemState.RESTORABLE.value
    assert item['media_file_id'] is None


# --- purge ------------------------------------------------------------------------

@pytest.mark.anyio
async def test_a_purge_destroys_the_file_frees_its_bytes_and_its_navidrome_row(
        trash, db_controller, beets, native, library_base):
    _track(db_controller, beets, native, library_base, 'a', 'A/a.mp3', size=300, media_id='mf-a')
    _track(db_controller, beets, native, library_base, 'keep', 'K/k.mp3', size=200, media_id='mf-k')
    _measure(db_controller)
    outcome = await trash.trash_tracks('dj', ['a'])
    native.go_missing('A/a.mp3')

    purge = await trash.purge_batch(outcome.batch_id, 'dj')

    assert purge.n_purged == 1 and not purge.errors
    assert not (library_base / '_trash' / 'dj' / outcome.batch_id).exists()
    # Exactly the file's size comes off.
    assert db_controller.library_bytes('dj') == 200
    assert db_controller.trash_bytes('dj') == 0
    assert 'mf-a' not in native.rows and 'mf-k' in native.rows
    [item] = db_controller.get_trash_batch(outcome.batch_id)['items']
    assert item['state'] == ItemState.PURGED.value and item['error'] is None


@pytest.mark.anyio
async def test_a_navidrome_row_that_survives_is_reported_not_hidden(
        trash, db_controller, beets, native, library_base):
    _track(db_controller, beets, native, library_base, 'a', 'A/a.mp3', media_id='mf-a')
    outcome = await trash.trash_tracks('dj', ['a'])
    native.go_missing('A/a.mp3')
    # Navidrome answers 200 to a purge it did not do (a non-admin token, say).
    native.undeletable.add('mf-a')

    await trash.purge_batch(outcome.batch_id, 'dj')

    [item] = db_controller.get_trash_batch(outcome.batch_id)['items']
    assert item['state'] == ItemState.PURGED.value
    assert 'navidrome row not purged' in item['error']


@pytest.mark.anyio
async def test_a_purge_never_takes_a_live_row(trash, db_controller, beets, native, library_base):
    # Re-uploaded at the same path during the window: the row is live again.
    _track(db_controller, beets, native, library_base, 'a', 'A/a.mp3', media_id='mf-a')
    outcome = await trash.trash_tracks('dj', ['a'])

    await trash.purge_batch(outcome.batch_id, 'dj')

    assert native.deleted == []
    assert 'mf-a' in native.rows


@pytest.mark.anyio
async def test_without_an_id_the_purge_matches_by_path_but_not_another_batchs_row(
        trash, db_controller, beets, native, library_base):
    native.fail_lookup = True
    _track(db_controller, beets, native, library_base, 'a', 'A/a.mp3')
    first = await trash.trash_tracks('dj', ['a'])
    native.fail_lookup = False
    native.add('mf-old', 'A/a.mp3', 'a', missing=True)
    # A later delete of a re-upload at the same path holds its own missing row.
    _track(db_controller, beets, native, library_base, 'a', 'A/a.mp3', media_id='mf-new')
    await trash.trash_tracks('dj', ['a'])
    native.go_missing('A/a.mp3')

    await trash.purge_batch(first.batch_id, 'dj')

    assert native.deleted == ['mf-old']
    assert 'mf-new' in native.rows


@pytest.mark.anyio
async def test_another_user_cannot_purge_a_batch(trash, db_controller, beets, native, library_base):
    _track(db_controller, beets, native, library_base, 'a', 'A/a.mp3')
    with db_controller._session_factory() as session:
        session.add(UserRow(username="other", password="pw", email="o@example.com", user_id="user-2",
                            beets_port=3, subsonic_port=4, max_library_size=MAX_BYTES))
        session.commit()
    outcome = await trash.trash_tracks('dj', ['a'])

    purge = await trash.purge_batch(outcome.batch_id, 'other')

    assert purge.n_purged == 0 and purge.errors
    assert db_controller.trash_bytes('dj') == 100


@pytest.mark.anyio
async def test_a_nodes_batch_is_left_for_playlist_delete_to_purge(trash, db_controller):
    batch_id = db_controller.create_trash_batch('dj', 'nodes', 'Folder House', RETENTION_S, [
        {'state': ItemState.RESTORABLE.value, 'snapshot': {'node_id': 1}},
    ])

    purge = await trash.purge_batch(batch_id)

    assert purge.errors and purge.n_purged == 0
    [item] = db_controller.get_trash_batch(batch_id)['items']
    assert item['state'] == ItemState.RESTORABLE.value


# --- the reaper's other jobs ------------------------------------------------------

def _iso(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f') + '123Z'


@pytest.mark.anyio
async def test_the_sweep_purges_stale_rows_no_batch_holds(trash, db_controller, beets, native, library_base):
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    native.add('stale', 'X/x.mp3', 'x', missing=True, updated_at=_iso(now - 2 * 86400))
    native.add('recent', 'Y/y.mp3', 'y', missing=True, updated_at=_iso(now - 3600))
    native.add('live', 'Z/z.mp3', 'z')
    _track(db_controller, beets, native, library_base, 'a', 'A/a.mp3', media_id='held')
    await trash.trash_tracks('dj', ['a'])
    native.go_missing('A/a.mp3', updated_at=_iso(now - 3 * 86400))

    swept = await trash.sweep_missing('dj', older_than_s=86400)

    assert swept == 1
    assert set(native.rows) == {'recent', 'live', 'held'}


def test_an_interrupted_delete_is_settled_by_where_its_files_are(trash, db_controller, beets, library_base):
    library = library_base / 'dj'
    batch_id = db_controller.create_trash_batch('dj', 'track', '4 tracks', RETENTION_S, [
        {'state': 'pending', 'subbox_id': s, 'relative_path': f'{s}.mp3', 'size': 1} for s in 'abcd'
    ])
    destination = library_base / '_trash' / 'dj' / batch_id
    destination.mkdir(parents=True)
    (destination / 'a.mp3').write_bytes(b'x')   # moved, and beets let go: restorable
    (destination / 'b.mp3').write_bytes(b'x')   # moved, but beets still has it
    beets.add('b', 'b.mp3')
    (library / 'c.mp3').write_bytes(b'x')       # never moved: the trash never held it
    # d is nowhere.

    trash.settle_pending(batch_id)

    states = {i['subbox_id']: i['state'] for i in db_controller.get_trash_batch(batch_id)['items']}
    assert states == {'a': 'restorable', 'b': 'failed', 'd': 'lost'}


def test_a_batch_is_as_live_as_its_liveliest_item():
    assert batch_state(['purged', 'restorable']) == 'restorable'
    assert batch_state(['restored', 'restoring']) == 'restoring'
    assert batch_state(['purged', 'purged']) == 'purged'
    assert batch_state(['restored']) == 'restored'
    assert batch_state(['purged', 'lost']) == 'failed'
