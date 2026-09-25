"""
An import stages this attempt's files and nothing else, and clears uploads/
whatever its outcome (laker-93/pymix#38).

The case these were written against is the prod one from 2026-09-23. An upload
of ~110 tracks mostly failed and never reached /sync/map_meta, so nothing it
left in uploads/ was tagged. The next attempt, for a different 17-track
playlist, uploaded nothing of its own, and the import staged the 82 leftovers
into beets with no SUBBOX_ID. Nothing could match or delete them by id.

Real FileBrowserFileHandler on a tmp dir, real DbController on sqlite, the real
map_meta and run_import_task. Only the import controllers are mocked: what they
are handed is the thing under test.
"""
import shutil
from pathlib import Path
from unittest import mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from pymix.controllers.db_controller import DbController
from pymix.handlers.filebrowser_file_handler import FileBrowserFileHandler
from pymix.handlers.rb_backup_file_handler import RBBackupFileHandler
from pymix.model.db_tables import Base, UserRow
from pymix.model.original_track_meta import OriginalTrackMeta, OriginalTracks, UploadAttempt
from pymix.routers import rb_import_export, serato_import_export
from pymix.routers.sync import map_meta
from pymix.services.import_progress import ImportPhase
from pymix.services.job_outcome import JobOutcome, Verdict, with_warning
from pymix.utils.tag_subbox_id import get_subbox_id, tag_subbox_id

USER = {'username': 'dj'}
FIXTURE_MP3 = Path(__file__).parent.parent / 'fixtures' / 'audio' / 'tagged.mp3'


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    controller = DbController(session_factory=sessionmaker(bind=engine), app_env="test", max_library_size=0)
    with controller._session_factory() as session:
        session.add(UserRow(
            username="dj", password="pw", email="dj@example.com", user_id="user-1",
            beets_port=1, subsonic_port=2, max_library_size=0,
        ))
        session.commit()
    return controller


@pytest.fixture
def handler(tmp_path, db):
    return FileBrowserFileHandler(
        local_user_music_stem='music',
        zip_name='music',
        serving_music_path_base=str(tmp_path / 'private-music'),
        filebrowser_data_path_uploads=str(tmp_path / '{user}' / 'uploads'),
        filebrowser_data_path_watch=str(tmp_path / '{user}' / 'watch'),
        filebrowser_data_path_downloads=str(tmp_path / '{user}' / 'downloads'),
        beets_data_path=str(tmp_path / 'beets' / '{user}'),
        beets_data_path_public=str(tmp_path / 'beets' / 'public'),
        update_job_period_s=60,
        db_controller=db,
    )


@pytest.fixture
def uploads(tmp_path) -> Path:
    path = tmp_path / 'dj' / 'uploads'
    path.mkdir(parents=True)
    return path


def _mp3(path: Path) -> Path:
    """A real, untagged mp3: what an upload that never reached map_meta leaves."""
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURE_MP3, path)
    return path


def _leftovers(uploads: Path, n: int = 3) -> list[Path]:
    return [_mp3(uploads / 'Old Artist' / 'Abandoned' / f'Track {i}.mp3') for i in range(n)]


async def _map_meta(db, handler, *staging_locations: str):
    """What the client sends after uploading an attempt's files."""
    tracks = OriginalTracks(tracks=[
        OriginalTrackMeta(
            userLocation=f'/Users/dj/Music/{loc}',
            stagingLocation=loc,
            originalName=Path(loc).stem,
            originalArtist='New Artist',
        )
        for loc in staging_locations
    ])
    return await map_meta(tracks=tracks, user=USER, db_controller=db, fb_file_handler=handler)


def _files_in(directory: Path) -> set[str]:
    return {str(f.relative_to(directory)) for f in directory.rglob('*') if f.is_file()}


def _controller_that_imports(record: dict, side_effect=None):
    """
    Stands in for RekordboxXMLController: remembers what it was asked to stage,
    and records a clean mapping phase so the verdict has evidence to go on.
    """
    async def create(**kwargs):
        record.update(kwargs)
        progress = kwargs['progress']
        progress.start_phase(ImportPhase.MAPPING_IDS, len(kwargs['audio_files']) or 1)
        progress.ok(len(kwargs['audio_files']) or 1)
        if side_effect:
            side_effect()
    controller = mock.Mock()
    controller.create_subsonic_playlists_from_xml = mock.AsyncMock(side_effect=create)
    return controller


def _job_db(db):
    """The real DbController for the upload attempt; the job row is mocked out."""
    job_db = mock.Mock(wraps=db)
    job_db.job_completed = mock.Mock()
    job_db.update_job_phase = mock.Mock()
    return job_db


async def _rb_import(db, handler, controller, playlist_names=None, xml_name=None):
    job_db = _job_db(db)
    attempt = db.get_upload_attempt('dj')
    size = handler.get_size_of_import('dj', attempt)
    await rb_import_export.run_import_task(
        controller, 'dj', 'job-1', job_db, handler, size['n_tracks'], USER, playlist_names, attempt,
        xml_name,
    )
    _, outcome = job_db.job_completed.call_args.args
    return size, outcome


# ------------------------------------------------------------------ the prod repro


@pytest.mark.anyio
async def test_leftovers_from_an_abandoned_upload_are_not_imported(db, handler, uploads):
    leftovers = _leftovers(uploads, n=3)
    _mp3(uploads / 'New Artist' / 'Set 1' / 'Opener.mp3')
    (uploads / 'rekordbox.xml').write_text('<DJ_PLAYLISTS/>')
    await _map_meta(db, handler, 'New Artist/Set 1/Opener.mp3')
    record = {}

    size, outcome = await _rb_import(db, handler, _controller_that_imports(record))

    assert size['n_tracks'] == 1, 'the leftovers must not inflate the count or the quota check'
    assert record['audio_files'] == [uploads / 'New Artist' / 'Set 1' / 'Opener.mp3']
    assert record['audio_path'] == uploads
    assert record['zip_path'] is None
    for leftover in leftovers:
        assert leftover not in record['audio_files']
    assert outcome.verdict is Verdict.SUCCESS


@pytest.mark.anyio
async def test_the_import_uses_the_xml_it_names_beside_a_leftover_one(db, handler, uploads):
    # The #192 shape: an earlier attempt uploaded its XML and stopped before it
    # started a job, so its XML was never cleared.
    (uploads / 'sep_2026_rev3.xml').write_text('<DJ_PLAYLISTS/>')
    (uploads / 'this_run.xml').write_text('<DJ_PLAYLISTS/>')
    await _map_meta(db, handler)
    record = {}

    _, outcome = await _rb_import(db, handler, _controller_that_imports(record), xml_name='this_run.xml')

    assert record['xml_path'] == uploads / 'this_run.xml'
    assert outcome.verdict is Verdict.SUCCESS
    assert list(uploads.iterdir()) == [], 'both XMLs go, so the next attempt starts clean'


@pytest.mark.anyio
async def test_an_import_naming_an_xml_that_is_not_there_fails_rather_than_guessing(db, handler, uploads):
    (uploads / 'leftover.xml').write_text('<DJ_PLAYLISTS/>')
    await _map_meta(db, handler)
    controller = mock.Mock()
    controller.create_subsonic_playlists_from_xml = mock.AsyncMock()

    _, outcome = await _rb_import(db, handler, controller, xml_name='this_run.xml')

    assert outcome.verdict is Verdict.FAILURE
    assert 'this_run.xml' in outcome.reason
    controller.create_subsonic_playlists_from_xml.assert_not_called()


@pytest.mark.anyio
async def test_an_attempt_that_uploaded_nothing_stages_nothing(db, handler, uploads):
    # The prod run exactly: every upload failed, so map_meta named no files, and
    # the only audio in uploads/ belonged to the run before.
    _leftovers(uploads, n=3)
    (uploads / 'rekordbox.xml').write_text('<DJ_PLAYLISTS/>')
    await _map_meta(db, handler)
    record = {}

    size, _ = await _rb_import(db, handler, _controller_that_imports(record))

    assert size['n_tracks'] == 0
    assert record['audio_files'] == []
    assert record['audio_path'] is None, 'no audio_path means no beets import at all'


@pytest.mark.anyio
async def test_a_metadata_only_import_with_no_map_meta_stages_nothing(db, handler, uploads):
    _leftovers(uploads, n=2)
    (uploads / 'rekordbox.xml').write_text('<DJ_PLAYLISTS/>')
    record = {}

    await _rb_import(db, handler, _controller_that_imports(record))

    assert record['audio_path'] is None


# ------------------------------------------------------------------ always clear


@pytest.mark.anyio
async def test_a_successful_import_clears_uploads_and_the_attempt(db, handler, uploads):
    _leftovers(uploads)
    _mp3(uploads / 'New Artist' / 'Set 1' / 'Opener.mp3')
    (uploads / 'rekordbox.xml').write_text('<DJ_PLAYLISTS/>')
    await _map_meta(db, handler, 'New Artist/Set 1/Opener.mp3')

    await _rb_import(db, handler, _controller_that_imports({}))

    assert list(uploads.iterdir()) == [], 'the files and the empty folders under them'
    assert db.get_upload_attempt('dj').files == {}


@pytest.mark.anyio
async def test_a_failed_import_clears_uploads_too(db, handler, uploads):
    # The half that made the leftovers: cleanup used to run on success only, so
    # a failed attempt left everything for the next one.
    _leftovers(uploads)
    _mp3(uploads / 'New Artist' / 'Set 1' / 'Opener.mp3')
    (uploads / 'rekordbox.xml').write_text('<DJ_PLAYLISTS/>')
    await _map_meta(db, handler, 'New Artist/Set 1/Opener.mp3')
    controller = mock.Mock()
    controller.create_subsonic_playlists_from_xml = mock.AsyncMock(side_effect=RuntimeError('beets fell over'))

    _, outcome = await _rb_import(db, handler, controller)

    assert outcome.verdict is Verdict.FAILURE
    assert 'beets fell over' in outcome.reason
    assert list(uploads.iterdir()) == []
    assert db.get_upload_attempt('dj').files == {}


@pytest.mark.anyio
async def test_an_import_that_fails_before_staging_still_clears_uploads(db, handler, uploads):
    # No XML: get_xml_data_path asserts before anything is staged (the #47 shape).
    # With success-only cleanup this is the wedge: the next attempt fails the same way.
    _leftovers(uploads)
    controller = mock.Mock()
    controller.create_subsonic_playlists_from_xml = mock.AsyncMock()

    _, outcome = await _rb_import(db, handler, controller)

    assert outcome.verdict is Verdict.FAILURE
    controller.create_subsonic_playlists_from_xml.assert_not_called()
    assert list(uploads.iterdir()) == []


@pytest.mark.anyio
async def test_a_file_uploaded_while_the_import_runs_survives_it(db, handler, uploads):
    _mp3(uploads / 'New Artist' / 'Set 1' / 'Opener.mp3')
    (uploads / 'rekordbox.xml').write_text('<DJ_PLAYLISTS/>')
    await _map_meta(db, handler, 'New Artist/Set 1/Opener.mp3')
    late = uploads / 'Next Artist' / 'Set 2' / 'Closer.mp3'

    def next_attempt_starts_uploading():
        _mp3(late)
        db.replace_upload_attempt('dj', {'Next Artist/Set 2/Closer.mp3': 'id-of-the-next-attempt'})

    await _rb_import(db, handler, _controller_that_imports({}, side_effect=next_attempt_starts_uploading))

    assert _files_in(uploads) == {'Next Artist/Set 2/Closer.mp3'}
    assert db.get_upload_attempt('dj').files == {'Next Artist/Set 2/Closer.mp3': 'id-of-the-next-attempt'}


# ------------------------------------------------------------------ refused files


@pytest.mark.anyio
async def test_an_attempt_file_that_lost_its_tag_is_refused_and_reported(db, handler, uploads):
    _mp3(uploads / 'New Artist' / 'Set 1' / 'Opener.mp3')
    _mp3(uploads / 'New Artist' / 'Set 1' / 'Closer.mp3')
    (uploads / 'rekordbox.xml').write_text('<DJ_PLAYLISTS/>')
    await _map_meta(db, handler, 'New Artist/Set 1/Opener.mp3', 'New Artist/Set 1/Closer.mp3')
    # The id map_meta returned never made it into the file (tag_subbox_id can
    # return an id it failed to write), modelled here by replacing the file.
    _mp3(uploads / 'New Artist' / 'Set 1' / 'Closer.mp3')
    record = {}

    size, outcome = await _rb_import(db, handler, _controller_that_imports(record))

    assert record['audio_files'] == [uploads / 'New Artist' / 'Set 1' / 'Opener.mp3']
    # Sizing runs in the request and doesn't read tags back, so it counts both.
    # Only staging refuses the one that lost its tag.
    assert size['n_tracks'] == 2
    assert outcome.verdict is Verdict.PARTIAL
    assert '1 uploaded track(s) were not imported' in outcome.warnings
    assert 'Closer.mp3' in outcome.warnings


def test_an_attempt_file_missing_from_uploads_is_refused(db, handler, uploads):
    attempt = UploadAttempt(files={'New Artist/Set 1/Gone.mp3': 'some-id'})

    selected = handler.select_attempt_files('dj', attempt)

    assert selected.files == []
    assert selected.refused == [('New Artist/Set 1/Gone.mp3', 'file is no longer in uploads')]


def test_non_audio_is_neither_staged_nor_a_leftover(db, handler, uploads):
    (uploads / 'rekordbox.xml').write_text('<DJ_PLAYLISTS/>')
    (uploads / 'all-crates.zip').write_bytes(b'PK\x05\x06' + b'\x00' * 18)

    selected = handler.select_attempt_files('dj', UploadAttempt(files={}))

    assert selected.files == [] and selected.leftovers == [] and selected.refused == []


# ------------------------------------------------------------------ map_meta


@pytest.mark.anyio
async def test_map_meta_records_the_files_it_tagged(db, handler, uploads):
    opener = _mp3(uploads / 'New Artist' / 'Set 1' / 'Opener.mp3')

    await _map_meta(db, handler, 'New Artist/Set 1/Opener.mp3')

    assert db.get_upload_attempt('dj').files == {'New Artist/Set 1/Opener.mp3': get_subbox_id(opener)}


@pytest.mark.anyio
async def test_a_new_map_meta_replaces_the_previous_attempt(db, handler, uploads):
    _mp3(uploads / 'Old Artist' / 'Set 0' / 'Track.mp3')
    await _map_meta(db, handler, 'Old Artist/Set 0/Track.mp3')
    _mp3(uploads / 'New Artist' / 'Set 1' / 'Opener.mp3')

    await _map_meta(db, handler, 'New Artist/Set 1/Opener.mp3')

    assert list(db.get_upload_attempt('dj').files) == ['New Artist/Set 1/Opener.mp3']


def test_clearing_an_attempt_leaves_one_recorded_after_it_was_read(db):
    db.replace_upload_attempt('dj', {'a.mp3': 'id-a'})
    read = db.get_upload_attempt('dj')
    db.replace_upload_attempt('dj', {'b.mp3': 'id-b'})

    db.clear_upload_attempt('dj', read)

    assert db.get_upload_attempt('dj').files == {'b.mp3': 'id-b'}


# ------------------------------------------------------------------ staging


def test_staging_copies_only_the_files_it_is_given(tmp_path, uploads):
    mine = _mp3(uploads / 'New Artist' / 'Set 1' / 'Opener.mp3')
    tag_subbox_id(mine)
    _leftovers(uploads)
    db_controller = mock.Mock()
    db_controller.get_subbox_beet_map.return_value = None
    rb_handler = RBBackupFileHandler(
        rekordbox_xml_orchestrator=mock.Mock(),
        db_controller=db_controller,
        beets_data_path=str(tmp_path / 'beets' / '{user}'),
        beets_data_path_public=str(tmp_path / 'beets' / 'public'),
    )

    rb_handler.stage_for_import('dj', uploads, [mine])

    assert _files_in(tmp_path / 'beets' / 'dj') == {'New Artist/Set 1/Opener.mp3'}


# ------------------------------------------------------------------ Serato


def _serato_controller(record: dict, error: Exception = None):
    async def create(**kwargs):
        record.update(kwargs)
        if error:
            raise error
        return mock.Mock(crates_parsed=1, playlists_built=1, matched=1, skipped=[], warning=lambda: None)
    controller = mock.Mock()
    controller.create_subsonic_playlists_from_crates = mock.AsyncMock(side_effect=create)
    return controller


async def _serato_import(db, handler, controller):
    job_db = _job_db(db)
    attempt = db.get_upload_attempt('dj')
    await serato_import_export.run_import_task(
        controller, 'dj', 'job-1', job_db, handler, 0, USER, [], attempt,
    )
    return job_db.job_completed.call_args.args


@pytest.mark.anyio
async def test_serato_stages_only_the_attempt_and_a_failure_clears_its_crates(db, handler, uploads):
    # The Serato shape of the same bug: a failed import left its .crate files, and
    # the next import parsed them alongside its own.
    _leftovers(uploads)
    (uploads / 'all-crates.zip').write_bytes(b'PK\x05\x06' + b'\x00' * 18)
    _mp3(uploads / 'New Artist' / 'Crate' / 'Opener.mp3')
    await _map_meta(db, handler, 'New Artist/Crate/Opener.mp3')
    record = {}

    _, success, reason, _ = await _serato_import(db, handler, _serato_controller(record, RuntimeError('FLAC')))

    assert record['audio_files'] == [uploads / 'New Artist' / 'Crate' / 'Opener.mp3']
    assert record['zip_path'] is None
    assert success is False and 'FLAC' in reason
    assert list(uploads.iterdir()) == []
    assert db.get_upload_attempt('dj').files == {}


@pytest.mark.anyio
async def test_serato_reports_a_refused_file_as_a_warning(db, handler, uploads):
    (uploads / 'all-crates.zip').write_bytes(b'PK\x05\x06' + b'\x00' * 18)
    _mp3(uploads / 'New Artist' / 'Crate' / 'Opener.mp3')
    await _map_meta(db, handler, 'New Artist/Crate/Opener.mp3')
    _mp3(uploads / 'New Artist' / 'Crate' / 'Opener.mp3')  # the tag did not stick

    _, success, _, warnings = await _serato_import(db, handler, _serato_controller({}))

    assert success is True
    assert '1 uploaded track(s) were not imported' in warnings


# ------------------------------------------------------------------ with_warning


def test_a_warning_turns_a_clean_success_into_a_partial():
    outcome = with_warning(JobOutcome(Verdict.SUCCESS), '1 track was refused')
    assert outcome.verdict is Verdict.PARTIAL and outcome.warnings == '1 track was refused'


def test_a_warning_joins_existing_warnings_and_leaves_a_failure_failed():
    outcome = with_warning(JobOutcome(Verdict.FAILURE, reason='boom', warnings='earlier'), 'refused')
    assert outcome.verdict is Verdict.FAILURE
    assert outcome.reason == 'boom' and outcome.warnings == 'earlier; refused'


def test_no_warning_changes_nothing():
    outcome = JobOutcome(Verdict.SUCCESS)
    assert with_warning(outcome, '') is outcome
