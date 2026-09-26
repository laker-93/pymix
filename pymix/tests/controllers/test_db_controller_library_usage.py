"""
The storage quota reads a counter, not a walk of the library (#183).

`user_library_size_exceeded` used to rglob `/private-music/<user>` and stat() every
file on every upload, every import and every watch-dir event, and never looked at
staging at all -- so a failed import's leftovers were disk the quota could not see.
"""
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from pymix.controllers.db_controller import DbController
from pymix.model.db_tables import Base, UserRow
from pymix.services import metrics

MAX_BYTES = 1000


@pytest.fixture
def roots(tmp_path):
    library_base = tmp_path / 'private-music'
    staging_base = tmp_path / 'private-staged'
    (library_base / 'dj').mkdir(parents=True)
    (staging_base / 'dj').mkdir(parents=True)
    return library_base, staging_base


@pytest.fixture
def db_controller(roots):
    library_base, staging_base = roots
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    controller = DbController(
        session_factory=sessionmaker(bind=engine),
        app_env="test",
        max_library_size=MAX_BYTES,
        serving_music_path_base=str(library_base),
        staging_path=f'{staging_base}/{{user}}/',
    )
    with controller._session_factory() as session:
        session.add(UserRow(
            username="dj", password="pw", email="dj@example.com", user_id="user-1",
            beets_port=1, subsonic_port=2, max_library_size=MAX_BYTES,
        ))
        session.commit()
    return controller


def _write(path: Path, n_bytes: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'x' * n_bytes)
    return path


def _stored(db_controller) -> int | None:
    with db_controller._session_factory() as session:
        return session.query(UserRow).filter(UserRow.username == 'dj').one().bytes_used


def _drift(username: str) -> float | None:
    return metrics.REGISTRY.get_sample_value(
        'pymix_user_library_drift_bytes', {'username': username}
    )


def test_an_unmeasured_user_is_walked_once_and_the_figure_stored(db_controller, roots):
    library_base, _ = roots
    _write(library_base / 'dj' / 'Artist' / 'a.mp3', 300)
    assert _stored(db_controller) is None

    assert db_controller.library_bytes('dj') == 300
    assert _stored(db_controller) == 300


def test_the_check_reads_the_counter_not_the_disk(db_controller, roots):
    library_base, _ = roots
    db_controller.reconcile_library_bytes('dj')  # empty library -> 0
    # A file the counter was never told about: a walk would see it, the counter does not.
    _write(library_base / 'dj' / 'a.mp3', 900)

    exceeded, max_bytes, used = db_controller.user_library_size_exceeded('dj', 100)

    assert (exceeded, max_bytes, used) == (False, MAX_BYTES, 0)


def test_staging_residue_counts_against_the_quota(db_controller, roots):
    _, staging_base = roots
    db_controller.reconcile_library_bytes('dj')
    # What a failed import leaves in private-staged was invisible to the old walk.
    _write(staging_base / 'dj' / 'left-behind.mp3', 950)

    exceeded, _, used = db_controller.user_library_size_exceeded('dj', 100)

    assert used == 950
    assert exceeded is True


def test_an_import_adds_what_left_staging(db_controller, roots):
    library_base, staging_base = roots
    db_controller.reconcile_library_bytes('dj')
    staged = _write(staging_base / 'dj' / 'a.mp3', 200)
    _write(staging_base / 'dj' / 'b.mp3', 50)

    with db_controller.record_staged_import('dj'):
        # beets moves a.mp3 into the library and leaves b.mp3 behind
        target = library_base / 'dj' / 'Artist' / 'a.mp3'
        target.parent.mkdir(parents=True)
        staged.rename(target)

    assert _stored(db_controller) == 200
    # and the total is unchanged by the move: library 200 + staging 50
    assert db_controller.usage_bytes('dj') == 250


def test_a_failed_import_still_counts_what_it_landed(db_controller, roots):
    library_base, staging_base = roots
    db_controller.reconcile_library_bytes('dj')
    staged = _write(staging_base / 'dj' / 'a.mp3', 200)

    with pytest.raises(RuntimeError):
        with db_controller.record_staged_import('dj'):
            staged.rename(library_base / 'dj' / 'a.mp3')
            raise RuntimeError('beets died after the first file')

    assert _stored(db_controller) == 200


def test_a_delete_takes_off_only_what_it_removed(db_controller, roots):
    library_base, _ = roots
    removed = _write(library_base / 'dj' / 'a.mp3', 200)
    kept = _write(library_base / 'dj' / 'b.mp3', 300)
    db_controller.reconcile_library_bytes('dj')

    with db_controller.record_removals('dj', [removed, kept, library_base / 'dj' / 'never-existed.mp3']):
        removed.unlink()

    assert _stored(db_controller) == 300


def test_the_counter_never_goes_negative(db_controller):
    db_controller.reconcile_library_bytes('dj')
    db_controller.add_library_bytes('dj', 100)

    db_controller.add_library_bytes('dj', -500)

    assert _stored(db_controller) == 0


def test_a_delta_on_an_unmeasured_user_leaves_it_unmeasured(db_controller, roots):
    library_base, _ = roots
    _write(library_base / 'dj' / 'a.mp3', 300)

    db_controller.add_library_bytes('dj', 100)

    # Still NULL, so the next read measures the truth rather than trusting 100.
    assert _stored(db_controller) is None
    assert db_controller.library_bytes('dj') == 300


def test_reconcile_replaces_a_drifted_counter(db_controller, roots):
    library_base, _ = roots
    _write(library_base / 'dj' / 'a.mp3', 300)
    db_controller.reconcile_library_bytes('dj')
    db_controller.add_library_bytes('dj', 5000)  # e.g. embedded art the counter missed, the other way round

    assert db_controller.reconcile_library_bytes('dj') == 300
    assert _stored(db_controller) == 300
    # Signed, disk minus counter: the counter had 5000 the disk did not.
    assert _drift('dj') == -5000


def test_a_reconcile_that_finds_the_counter_right_records_zero_drift(db_controller, roots):
    """Not the previous figure: a gauge left at the last non-zero drift would keep
    reporting a problem the reconcile had just shown was gone."""
    library_base, _ = roots
    _write(library_base / 'dj' / 'a.mp3', 300)
    db_controller.reconcile_library_bytes('dj')
    db_controller.add_library_bytes('dj', 7)
    db_controller.reconcile_library_bytes('dj')
    assert _drift('dj') == -7

    db_controller.reconcile_library_bytes('dj')

    assert _drift('dj') == 0


def test_the_first_measurement_is_not_drift(db_controller, roots):
    """A NULL counter was never a claim about the disk, so its first walk -- every
    user's, right after migration 021 -- has nothing to have drifted from."""
    library_base, _ = roots
    metrics.user_library_drift_bytes.clear()
    _write(library_base / 'dj' / 'a.mp3', 300)

    db_controller.reconcile_library_bytes('dj')

    assert _drift('dj') is None


def test_storage_usage_is_read_per_user_as_stored(db_controller, roots):
    library_base, _ = roots
    assert db_controller.storage_usage_by_user() == [('dj', None, MAX_BYTES)]

    _write(library_base / 'dj' / 'a.mp3', 300)
    db_controller.reconcile_library_bytes('dj')

    assert db_controller.storage_usage_by_user() == [('dj', 300, MAX_BYTES)]


def test_a_file_vanishing_mid_walk_is_skipped(db_controller, roots, monkeypatch):
    library_base, _ = roots
    _write(library_base / 'dj' / 'a.mp3', 300)
    gone = _write(library_base / 'dj' / 'b.mp3', 50)
    real_stat = Path.stat

    def stat(self, *args, **kwargs):
        if self == gone and not kwargs and not args:
            raise FileNotFoundError(self)
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'stat', stat)

    assert db_controller.reconcile_library_bytes('dj') == 300
