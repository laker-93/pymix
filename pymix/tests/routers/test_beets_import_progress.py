"""
What /beets/import/progress tells the client about a finished job.

Two defects are locked down here, both of which made an import unfalsifiable from
the outside (laker-93/subbox-app#48, #55):

* a job that genuinely ran and failed reported ``reason: ""``, so the only thing
  the user could be shown was the client's generic "Import failed";
* a job with no audio to import (a metadata-only Rekordbox re-import) took the
  "no in-progress jobs found" branch and reported 0% forever — which is what the
  client sees now that it polls that path to the end instead of assuming success.
"""
from unittest import mock

import pytest

from pymix.routers.beets_import import import_progress
from pymix.services.import_progress import ImportPhase, UNRECORDED_FAILURE_REASON

USER = {"username": "dj"}


def _job(**overrides):
    job = {
        "n_tracks_to_import": 10,
        "total_n_imported_tracks": 0,
        "in_progress": True,
        "result": None,
        "phase": ImportPhase.IMPORTING_AUDIO.value,
        "phase_n_processed": 0,
        "phase_n_total": 10,
        "reason": None,
    }
    job.update(overrides)
    return job


def _controllers(job, n_tracks_on_disk=0):
    db_controller = mock.Mock()
    db_controller.get_job_by_id.return_value = job
    beets_client = mock.Mock()
    beets_client.count_tracks_on_disk = mock.AsyncMock(return_value=n_tracks_on_disk)
    return db_controller, beets_client


async def _progress(job, n_tracks_on_disk=0):
    db_controller, beets_client = _controllers(job, n_tracks_on_disk)
    return await import_progress(
        job_id="job-1",
        public=False,
        user=USER,
        beets_client=beets_client,
        db_controller=db_controller,
    )


@pytest.mark.anyio
async def test_a_failed_job_reports_the_reason_it_recorded():
    job = _job(
        in_progress=False,
        result=False,
        phase=ImportPhase.APPLYING_METADATA.value,
        reason="KeyError: 'Rating'",
    )

    progress = await _progress(job, n_tracks_on_disk=10)

    assert progress["reason"] == "KeyError: 'Rating'"


@pytest.mark.anyio
async def test_a_failed_job_never_reports_an_empty_reason():
    # A job that failed before it could record anything, or one written before
    # migration 017. The client's fallback string is not an explanation, so the
    # server owes it something that at least says the reason is unknown.
    job = _job(in_progress=False, result=False, reason=None)

    progress = await _progress(job, n_tracks_on_disk=10)

    assert progress["reason"] == UNRECORDED_FAILURE_REASON


@pytest.mark.anyio
async def test_a_failed_job_keeps_the_phase_it_died_in():
    # Which pass broke is what tells the user whether their tracks are in the
    # library — the endpoint must pass the stored phase through, not COMPLETE.
    job = _job(in_progress=False, result=False, phase=ImportPhase.APPLYING_METADATA.value)

    progress = await _progress(job, n_tracks_on_disk=10)

    assert progress["phase"] == ImportPhase.APPLYING_METADATA.value


@pytest.mark.anyio
async def test_a_running_job_reports_no_reason():
    progress = await _progress(_job(), n_tracks_on_disk=3)

    assert progress["reason"] == ""
    assert progress["in_progress"] is True


@pytest.mark.anyio
async def test_a_metadata_only_job_makes_progress_instead_of_sitting_at_zero():
    # No audio to land, so the only thing moving is the tail passes' own counters.
    job = _job(
        n_tracks_to_import=0,
        phase=ImportPhase.APPLYING_METADATA.value,
        phase_n_processed=50,
        phase_n_total=100,
    )

    progress = await _progress(job)

    assert 0 < progress["percentage_complete"] < 100
    # ...and it is not described as a job that doesn't exist.
    assert progress["reason"] == ""


@pytest.mark.anyio
async def test_a_metadata_only_job_reads_100_once_it_succeeds():
    job = _job(
        n_tracks_to_import=0,
        in_progress=False,
        result=True,
        phase=ImportPhase.COMPLETE.value,
    )

    progress = await _progress(job)

    assert progress["percentage_complete"] == 100
    assert progress["reason"] == ""


@pytest.mark.anyio
async def test_a_metadata_only_job_that_fails_still_reports_its_reason():
    job = _job(
        n_tracks_to_import=0,
        in_progress=False,
        result=False,
        phase=ImportPhase.APPLYING_METADATA.value,
        reason="RuntimeError: navidrome scan timed out",
    )

    progress = await _progress(job)

    assert progress["reason"] == "RuntimeError: navidrome scan timed out"


@pytest.mark.anyio
async def test_a_metadata_only_job_never_shells_into_the_beets_container():
    # There is no audio count to take, and the poll runs every 3s for the whole job.
    db_controller, beets_client = _controllers(_job(n_tracks_to_import=0))

    await import_progress(
        job_id="job-1",
        public=False,
        user=USER,
        beets_client=beets_client,
        db_controller=db_controller,
    )

    beets_client.count_tracks_on_disk.assert_not_awaited()
