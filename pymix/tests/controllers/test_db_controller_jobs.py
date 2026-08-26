"""
What a finished import job records about itself (laker-93/subbox-app#48).

`job_completed` used to write only in_progress/result and force the phase to
COMPLETE, which threw away both halves of what a failure screen needs: why it
broke, and which pass it broke in.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from pymix.controllers.db_controller import DbController
from pymix.model.db_tables import Base, UserRow
from pymix.services.import_progress import ImportPhase


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
