"""
What a finished import job records about itself (laker-93/subbox-app#48).

`job_completed` used to write only in_progress/result and force the phase to
COMPLETE, which threw away both halves of what a failure screen needs: why it
broke, and which pass it broke in.
"""
from unittest import mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from pymix.controllers.db_controller import DbController
from pymix.model.db_tables import Base, UserRow
from pymix.services.import_progress import ImportPhase, ImportProgressReporter
from pymix.services.job_outcome import OutcomeLedger


@pytest.fixture
def db_controller():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    controller = DbController(
        session_factory=sessionmaker(bind=engine),
        app_env="test",
        max_library_size=0,
    )
    # Inserted directly rather than via create_user, which allocates host ports and
    # consumes a signup token — neither of which a job has anything to do with.
    with controller._session_factory() as session:
        session.add(UserRow(
            username="dj",
            password="pw",
            email="dj@example.com",
            user_id="user-1",
            beets_port=1,
            subsonic_port=2,
            max_library_size=0,
        ))
        session.commit()
    return controller


@pytest.fixture
def job_id(db_controller):
    return db_controller.create_import_job("dj", number_of_tracks_to_import=5, total_n_imported_tracks=0)


def _job(db_controller, job_id):
    return db_controller.get_job_by_id("dj", job_id)


def test_a_failed_job_stores_its_reason(db_controller, job_id):
    db_controller.job_completed(job_id, False, "KeyError: 'Rating'")

    job = _job(db_controller, job_id)
    assert job["result"] is False
    assert job["in_progress"] is False
    assert job["reason"] == "KeyError: 'Rating'"


def test_a_failed_job_keeps_the_phase_it_died_in(db_controller, job_id):
    db_controller.update_job_phase(job_id, ImportPhase.APPLYING_METADATA.value, 3, 5)

    db_controller.job_completed(job_id, False, "boom")

    # Not COMPLETE: the tracks landed and the metadata pass is what broke, and the
    # user is told those two things differently.
    assert _job(db_controller, job_id)["phase"] == ImportPhase.APPLYING_METADATA.value


def test_a_successful_job_completes_with_no_reason(db_controller, job_id):
    db_controller.update_job_phase(job_id, ImportPhase.APPLYING_METADATA.value, 5, 5)

    db_controller.job_completed(job_id, True)

    job = _job(db_controller, job_id)
    assert job["result"] is True
    assert job["phase"] == ImportPhase.COMPLETE.value
    assert job["reason"] is None


def test_a_successful_job_can_still_carry_a_warning(db_controller, job_id):
    """
    A Serato import whose crates named tracks the user never uploaded succeeded —
    but not on all of it. `reason` never reaches the client on a successful job,
    so reporting that through `reason` would be the same as saying nothing, and
    reporting nothing is how a job comes back result=true with work missing.
    """
    db_controller.job_completed(
        job_id, True, warnings="2 of 10 tracks in your crates could not be matched."
    )

    job = _job(db_controller, job_id)
    assert job["result"] is True
    assert job["reason"] is None
    assert job["warnings"] == "2 of 10 tracks in your crates could not be matched."


def test_a_clean_job_has_no_warnings(db_controller, job_id):
    db_controller.job_completed(job_id, True)

    assert _job(db_controller, job_id)["warnings"] is None


# --- the verdict is computed, not asserted (laker-93/pymix#171) ---------------


def test_a_computed_failure_lands_on_the_row_with_its_reason(db_controller, job_id):
    # Through the reporter, as the import does, so the row's phase comes from the
    # same call that opened the ledger entry.
    progress = ImportProgressReporter(db_controller, job_id)
    progress.start_phase(ImportPhase.APPLYING_METADATA, 8)
    for i in range(8):
        progress.failed(f"SBX-{i}", "container beetsdj is not running")

    db_controller.job_completed(job_id, progress.verdict())

    job = _job(db_controller, job_id)
    assert job["result"] is False
    assert "0 of 8" in job["reason"]
    # A failed job keeps the phase it died in, so the screen can say which pass.
    assert job["phase"] == ImportPhase.APPLYING_METADATA.value


def test_a_computed_partial_succeeds_but_carries_its_warning(db_controller, job_id):
    ledger = OutcomeLedger()
    ledger.start_phase(ImportPhase.APPLYING_METADATA, 3)
    ledger.ok(2)
    ledger.skipped("Track C", "no matching track in your library")

    db_controller.job_completed(job_id, ledger.verdict())

    job = _job(db_controller, job_id)
    assert job["result"] is True
    assert job["reason"] is None
    assert "1 skipped" in job["warnings"]
    assert job["phase"] == ImportPhase.COMPLETE.value


def test_a_partial_is_counted_apart_from_a_clean_success(db_controller, job_id):
    # Otherwise a half-failing import is indistinguishable from a good one on the
    # dashboard -- the operator-side version of the bug #171 is about.
    ledger = OutcomeLedger()
    ledger.start_phase(ImportPhase.APPLYING_METADATA, 3)
    ledger.ok(2)
    ledger.failed("SBX-C", "beets matched no track with this subbox_id")

    with mock.patch("pymix.controllers.db_controller.metrics") as metrics:
        db_controller.job_completed(job_id, ledger.verdict())

    assert metrics.job_finished.call_args == mock.call(job_id, True, outcome="partial")


def test_a_clean_success_keeps_the_label_it_always_had(db_controller, job_id):
    ledger = OutcomeLedger()
    ledger.start_phase(ImportPhase.MAPPING_IDS, 2)
    ledger.ok(2)

    with mock.patch("pymix.controllers.db_controller.metrics") as metrics:
        db_controller.job_completed(job_id, ledger.verdict())

    assert metrics.job_finished.call_args == mock.call(job_id, True, outcome=None)


def test_reason_and_warnings_may_not_be_passed_alongside_an_outcome(db_controller, job_id):
    # Two sources of truth for the same three fields is how they drift apart.
    with pytest.raises(AssertionError):
        db_controller.job_completed(job_id, OutcomeLedger().verdict(), "a reason")


# --- the counts reach the row (migration 019, subbox-app#50) ------------------


def test_the_phase_counts_are_persisted_with_the_verdict(db_controller, job_id):
    ledger = OutcomeLedger()
    ledger.start_phase(ImportPhase.MAPPING_IDS, 5)
    ledger.ok(5)
    ledger.start_phase(ImportPhase.APPLYING_METADATA, 5)
    ledger.ok(5)

    db_controller.job_completed(job_id, ledger.verdict())

    assert _job(db_controller, job_id)["phases"] == [
        {"phase": "mapping_ids", "total": 5, "ok": 5, "skipped": 0, "failed": 0},
        {"phase": "applying_metadata", "total": 5, "ok": 5, "skipped": 0, "failed": 0},
    ]


def test_a_job_completed_without_a_ledger_records_no_phases(db_controller, job_id):
    # The Serato import and the watch-dir handler still pass a bare bool. Writing
    # [] for them would claim they ran no phases; they ran unreported ones.
    db_controller.job_completed(job_id, True)

    assert _job(db_controller, job_id)["phases"] is None
