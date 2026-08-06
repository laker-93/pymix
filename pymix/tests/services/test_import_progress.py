"""
The progress an import job reports (laker-93/pymix#51).

The defect being locked down: the old percentage came only from beets' track
count, so it hit 100 the moment the last audio file landed and could not move
for the two post-import passes that follow -- ~13 minutes of "100%" on a
100-track prod import, which reads as a hung job.
"""
from unittest import mock

from pymix.services.import_progress import (
    ImportPhase,
    ImportProgressReporter,
    NullImportProgressReporter,
    overall_percentage,
    reporter_or_null,
)


def test_a_finished_audio_phase_does_not_report_100_percent():
    # Every audio file has landed, but the two tail passes have not run. This is
    # the exact state that used to render as a frozen 100%.
    pct = overall_percentage(ImportPhase.IMPORTING_AUDIO.value, phase_fraction=1.0, audio_fraction=1.0)

    assert pct < 100


def test_percentage_never_reaches_100_while_the_job_is_still_running():
    for phase in (ImportPhase.IMPORTING_AUDIO, ImportPhase.MAPPING_IDS, ImportPhase.APPLYING_METADATA):
        assert overall_percentage(phase.value, phase_fraction=1.0, audio_fraction=1.0) < 100


def test_the_complete_phase_is_the_only_one_that_reports_100():
    assert overall_percentage(ImportPhase.COMPLETE.value, 0.0, 0.0) == 100


def test_percentage_advances_through_the_tail_phases():
    audio_done = overall_percentage(ImportPhase.IMPORTING_AUDIO.value, 1.0, 1.0)
    mapping = overall_percentage(ImportPhase.MAPPING_IDS.value, 0.5, 1.0)
    metadata_start = overall_percentage(ImportPhase.APPLYING_METADATA.value, 0.0, 1.0)
    metadata_half = overall_percentage(ImportPhase.APPLYING_METADATA.value, 0.5, 1.0)

    assert audio_done < mapping < metadata_start < metadata_half


def test_percentage_tracks_the_audio_phase_from_the_beets_count():
    # The audio phase is the one phase whose progress is observable from outside
    # the job, so it still comes from the beets track count.
    assert overall_percentage(ImportPhase.IMPORTING_AUDIO.value, 0.0, 0.0) == 0
    quarter = overall_percentage(ImportPhase.IMPORTING_AUDIO.value, 0.0, 0.25)
    half = overall_percentage(ImportPhase.IMPORTING_AUDIO.value, 0.0, 0.5)
    assert 0 < quarter < half


def test_an_unknown_or_missing_phase_is_treated_as_the_audio_phase():
    # Jobs created before migration 016 have no phase, and must keep reporting
    # something sensible rather than blowing up a poll.
    assert overall_percentage(None, 0.0, 0.5) == overall_percentage(
        ImportPhase.IMPORTING_AUDIO.value, 0.0, 0.5
    )
    assert overall_percentage("something_new", 0.0, 0.5) == overall_percentage(
        ImportPhase.IMPORTING_AUDIO.value, 0.0, 0.5
    )


def test_an_overshooting_count_cannot_push_the_phase_past_its_own_slice():
    # Duplicate handling can make the beets delta exceed the number of tracks
    # asked for, so the fraction can come in above 1.
    assert overall_percentage(
        ImportPhase.IMPORTING_AUDIO.value, 0.0, 1.4
    ) == overall_percentage(ImportPhase.IMPORTING_AUDIO.value, 0.0, 1.0)


def test_reporter_records_the_phase_and_its_running_count():
    db_controller = mock.Mock()
    reporter = ImportProgressReporter(db_controller, "job-1")

    reporter.start_phase(ImportPhase.MAPPING_IDS, 3)
    reporter.advance()
    reporter.advance()

    assert db_controller.update_job_phase.call_args_list == [
        mock.call("job-1", phase="mapping_ids", n_processed=0, n_total=3),
        mock.call("job-1", phase="mapping_ids", n_processed=1, n_total=3),
        mock.call("job-1", phase="mapping_ids", n_processed=2, n_total=3),
    ]


def test_a_new_phase_resets_the_count():
    db_controller = mock.Mock()
    reporter = ImportProgressReporter(db_controller, "job-1")

    reporter.start_phase(ImportPhase.MAPPING_IDS, 3)
    reporter.advance()
    reporter.start_phase(ImportPhase.APPLYING_METADATA, 2)

    assert db_controller.update_job_phase.call_args_list[-1] == mock.call(
        "job-1", phase="applying_metadata", n_processed=0, n_total=2
    )


def test_a_failing_progress_write_never_propagates_into_the_import():
    # Progress is decoration; it must not be able to fail the import it describes.
    db_controller = mock.Mock()
    db_controller.update_job_phase.side_effect = RuntimeError("db gone")
    reporter = ImportProgressReporter(db_controller, "job-1")

    reporter.start_phase(ImportPhase.MAPPING_IDS, 3)
    reporter.advance()


def test_untracked_import_paths_get_a_no_op_reporter():
    reporter = reporter_or_null(None)

    assert isinstance(reporter, NullImportProgressReporter)
    reporter.start_phase(ImportPhase.MAPPING_IDS, 3)
    reporter.advance()


def test_an_explicit_reporter_is_passed_through():
    db_controller = mock.Mock()
    reporter = ImportProgressReporter(db_controller, "job-1")

    assert reporter_or_null(reporter) is reporter
