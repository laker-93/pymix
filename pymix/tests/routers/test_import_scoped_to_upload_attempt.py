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
import time
from pathlib import Path
from unittest import mock

import anyio
import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from pymix.controllers.db_controller import DbController
from pymix.handlers.filebrowser_file_handler import FileBrowserFileHandler
from pymix.handlers.rb_backup_file_handler import RBBackupFileHandler
from pymix.model.db_tables import Base, UserRow
from pymix.model.original_track_meta import OriginalTrackMeta, OriginalTracks, UploadAttempt
from pymix.model.playlist_write_report import PlaylistWriteReport
from pymix.routers import rb_import_export, serato_import_export
from pymix.routers.sync import map_meta, map_meta_progress, run_map_meta_job
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
    tasks = BackgroundTasks()
    response = await map_meta(
        tracks=tracks, background_tasks=tasks, user=USER, db_controller=db, fb_file_handler=handler,
    )
    # The route only starts the job (#237): run it to the end, as the server would.
    await tasks()
    return db.get_job_by_id('dj', response['job_id'])


def _files_in(directory: Path) -> set[str]:
    return {str(f.relative_to(directory)) for f in directory.rglob('*') if f.is_file()}


def _controller_that_imports(record: dict, side_effect=None, playlists=None):
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
        return playlists
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
async def test_a_playlist_the_reimport_held_back_is_a_warning_on_the_job(db, handler, uploads):
    # #203: the scan hadn't finished, so the user's existing playlist was left as it was.
    (uploads / 'rekordbox.xml').write_text('<DJ_PLAYLISTS/>')
    await _map_meta(db, handler)

    _, outcome = await _rb_import(
        db, handler, _controller_that_imports({}, playlists=PlaylistWriteReport(held_back=['Deep'])),
    )

    assert outcome.verdict is Verdict.PARTIAL
    assert outcome.warnings == "`Deep` not updated: the library scan hadn't finished. Re-import to update it."


@pytest.mark.anyio
async def test_what_a_reimport_replaced_is_kept_before_the_job_completes(db, handler, uploads):
    # #208: the client learns of the undo from the finished job, so the batch has to
    # be named on it by then.
    (uploads / 'rekordbox.xml').write_text('<DJ_PLAYLISTS/>')
    await _map_meta(db, handler)
    report = PlaylistWriteReport(updated=['Deep'])
    order = []
    trash_service = mock.Mock()
    trash_service.keep_replaced_entries.side_effect = lambda *args: order.append('kept')
    job_db = _job_db(db)
    job_db.job_completed.side_effect = lambda *args: order.append('completed')
    attempt = db.get_upload_attempt('dj')

    await rb_import_export.run_import_task(
        _controller_that_imports({}, playlists=report), 'dj', 'job-1', job_db, handler, 0, USER, None, attempt,
        None, trash_service,
    )

    trash_service.keep_replaced_entries.assert_called_once_with('dj', 'job-1', report)
    assert order == ['kept', 'completed']


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


@pytest.mark.anyio
async def test_map_meta_answers_before_tagging_and_the_job_reports_the_outcome(db, handler, uploads):
    # #237: tagging a large upload in the request outlived Cloudflare's 100s.
    opener = _mp3(uploads / 'New Artist' / 'Set 1' / 'Opener.mp3')
    tracks = OriginalTracks(tracks=[OriginalTrackMeta(
        userLocation='/Users/dj/Music/Opener.mp3', stagingLocation='New Artist/Set 1/Opener.mp3',
        originalName='Opener', originalArtist='New Artist',
    )])
    tasks = BackgroundTasks()

    response = await map_meta(tracks=tracks, background_tasks=tasks, user=USER, db_controller=db, fb_file_handler=handler)

    assert response['n_tracks'] == 1
    assert get_subbox_id(opener) is None, 'the request must not tag anything itself'
    running = await map_meta_progress(job_id=response['job_id'], user=USER, db_controller=db)
    assert running['in_progress'] is True and running['n_tracks'] == 1

    await tasks()

    done = await map_meta_progress(job_id=response['job_id'], user=USER, db_controller=db)
    assert done['in_progress'] is False and done['result'] is True and done['detail'] is None
    assert done['n_tracks_processed'] == 1
    assert db.get_upload_attempt('dj').files == {'New Artist/Set 1/Opener.mp3': get_subbox_id(opener)}


@pytest.mark.anyio
async def test_a_map_meta_job_that_could_not_tag_everything_says_which(db, handler, uploads):
    _mp3(uploads / 'New Artist' / 'Set 1' / 'Opener.mp3')

    job = await _map_meta(db, handler, 'New Artist/Set 1/Opener.mp3', 'New Artist/Set 1/Never Uploaded.mp3')

    assert job['in_progress'] is False and job['result'] is False
    assert job['detail']['untagged_count'] == 1
    assert job['detail']['untagged_tracks'][0]['stagingLocation'] == 'New Artist/Set 1/Never Uploaded.mp3'
    # As before: what was tagged is still the attempt, for a metadata-only retry.
    assert list(db.get_upload_attempt('dj').files) == ['New Artist/Set 1/Opener.mp3']


@pytest.mark.anyio
async def test_map_meta_tags_the_file_at_the_staging_location_and_no_other(db, handler, uploads):
    # The substring match gave `Koze/...` the id of `DJ Koze/...`'s file (#237).
    dj_koze = _mp3(uploads / 'DJ Koze' / 'Album' / 'T.mp3')
    koze = _mp3(uploads / 'Koze' / 'Album' / 'T.mp3')

    await _map_meta(db, handler, 'Koze/Album/T.mp3')

    assert get_subbox_id(dj_koze) is None
    assert db.get_upload_attempt('dj').files == {'Koze/Album/T.mp3': get_subbox_id(koze)}


@pytest.mark.anyio
async def test_map_meta_never_follows_a_staging_location_out_of_the_users_uploads(db, handler, uploads):
    theirs = _mp3(uploads.parent / 'someone-else' / 'T.mp3')

    job = await _map_meta(db, handler, '../someone-else/T.mp3')

    assert get_subbox_id(theirs) is None
    assert job['result'] is False
    assert db.get_upload_attempt('dj').files == {}


@pytest.mark.anyio
async def test_map_meta_tags_off_the_event_loop(db, uploads):
    # All of pymix, for every user, stalled for the whole of a large tagging run.
    def slow_tagging(user, tracks, on_progress=None):
        time.sleep(0.3)
        return {'staged': {}}
    slow_handler = mock.Mock()
    slow_handler.tag_staging_with_subbox_id = slow_tagging
    job_id = db.create_map_meta_job('dj', 0)
    ticks = 0

    async def tick():
        nonlocal ticks
        while True:
            ticks += 1
            await anyio.sleep(0.01)

    async with anyio.create_task_group() as tg:
        tg.start_soon(tick)
        await run_map_meta_job(db, slow_handler, 'dj', OriginalTracks(tracks=[]), job_id)
        tg.cancel_scope.cancel()

    assert ticks > 10


@pytest.mark.anyio
async def test_the_import_refuses_while_map_meta_is_still_tagging(db):
    db.create_map_meta_job('dj', 5)

    for route in (rb_import_export.rekordbox_import, serato_import_export.serato_import):
        with pytest.raises(HTTPException) as refused:
            await route(
                request=mock.Mock(), background_tasks=BackgroundTasks(), user=USER, beets_client=mock.Mock(),
                fb_file_handler=mock.Mock(), db_controller=db, config={}, trash_service=mock.Mock(),
                **({'rekordbox_xml_controller': mock.Mock()} if route is rb_import_export.rekordbox_import
                   else {'serato_controller': mock.Mock()}),
            )
        assert refused.value.status_code == 409


@pytest.mark.anyio
async def test_a_second_map_meta_waits_for_the_first(db, handler):
    db.create_map_meta_job('dj', 5)

    with pytest.raises(HTTPException) as refused:
        await map_meta(tracks=OriginalTracks(tracks=[]), background_tasks=BackgroundTasks(),
                       user=USER, db_controller=db, fb_file_handler=handler)

    assert refused.value.status_code == 409


def test_a_map_meta_job_is_not_the_users_one_job(db):
    # The watch dir can be importing while an upload is tagged: the one-job
    # helpers (export progress, trash restore) must not trip over it.
    db.create_map_meta_job('dj', 5)

    assert db.get_number_of_jobs('dj', in_progress=True) == 0


def test_a_restart_fails_the_map_meta_job_it_interrupted(db):
    job_id = db.create_map_meta_job('dj', 5)

    assert db.fail_interrupted_map_meta_jobs() == 1

    job = db.get_job_by_id('dj', job_id)
    assert job['in_progress'] is False and job['result'] is False and 'restarted' in job['reason']
    assert db.get_in_progress_map_meta_job('dj') is None


def test_staged_sizes_are_what_the_server_holds_and_never_outside_the_users_uploads(handler, uploads):
    whole = _mp3(uploads / 'New Artist' / 'Set 1' / 'Opener.mp3')
    _mp3(uploads.parent / 'someone-else' / 'T.mp3')

    sizes = handler.staged_file_sizes('dj', ['New Artist/Set 1/Opener.mp3', 'New Artist/Set 1/Missing.mp3', '../someone-else/T.mp3'])

    assert sizes == {
        'New Artist/Set 1/Opener.mp3': whole.stat().st_size,
        'New Artist/Set 1/Missing.mp3': None,
        '../someone-else/T.mp3': None,
    }


def test_clearing_an_attempt_leaves_one_recorded_after_it_was_read(db):
    db.replace_upload_attempt('dj', {'a.mp3': 'id-a'})
    read = db.get_upload_attempt('dj')
    db.replace_upload_attempt('dj', {'b.mp3': 'id-b'})

    db.clear_upload_attempt('dj', read)

    assert db.get_upload_attempt('dj').files == {'b.mp3': 'id-b'}


# ------------------------------------------------------------------ staging


def _rb_handler(tmp_path) -> RBBackupFileHandler:
    db_controller = mock.Mock()
    db_controller.get_subbox_beet_map.return_value = None
    return RBBackupFileHandler(
        rekordbox_xml_orchestrator=mock.Mock(),
        db_controller=db_controller,
        beets_data_path=str(tmp_path / 'beets' / '{user}'),
        beets_data_path_public=str(tmp_path / 'beets' / 'public'),
    )


def test_staging_moves_only_the_files_it_is_given(tmp_path, uploads):
    mine = _mp3(uploads / 'New Artist' / 'Set 1' / 'Opener.mp3')
    tag_subbox_id(mine)
    _leftovers(uploads)

    _rb_handler(tmp_path).stage_for_import('dj', uploads, [mine])

    assert _files_in(tmp_path / 'beets' / 'dj') == {'New Artist/Set 1/Opener.mp3'}
    # Moved, not copied: a copy held the whole upload on disk twice until the
    # import finished, and a large library filled the volume mid-staging.
    assert not mine.exists()
    assert _files_in(uploads) == {f'Old Artist/Abandoned/Track {i}.mp3' for i in range(3)}


def test_staging_a_whole_directory_moves_every_file(tmp_path, uploads):
    files = [_mp3(uploads / 'A' / 'X' / f'{i}.mp3') for i in range(3)]
    for f in files:
        tag_subbox_id(f)

    _rb_handler(tmp_path).stage_for_import('dj', uploads)

    assert _files_in(tmp_path / 'beets' / 'dj') == {f'A/X/{i}.mp3' for i in range(3)}
    assert _files_in(uploads) == set()


def test_discard_staging_empties_the_users_staging_only(tmp_path):
    _mp3(tmp_path / 'beets' / 'dj' / 'A' / 'X' / 'a.mp3')
    _mp3(tmp_path / 'beets' / 'dj' / 'b.mp3')
    _mp3(tmp_path / 'beets' / 'other' / 'c.mp3')

    _rb_handler(tmp_path).discard_staging('dj')

    assert list((tmp_path / 'beets' / 'dj').iterdir()) == []
    assert _files_in(tmp_path / 'beets' / 'other') == {'c.mp3'}


def test_discard_staging_never_raises(tmp_path):
    handler = _rb_handler(tmp_path)
    handler.discard_staging('nobody')  # no staging directory at all

    _mp3(tmp_path / 'beets' / 'dj' / 'a.mp3')
    with mock.patch('pymix.handlers.rb_backup_file_handler.Path.unlink', side_effect=PermissionError):
        handler.discard_staging('dj')


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
