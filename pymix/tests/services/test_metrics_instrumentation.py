"""The instrumentation added on top of the scrape endpoint: the funnel, engagement,
and the three places pymix's real work happens.

`test_metrics.py` covers the endpoint itself -- that it is authenticated and that it
renders. This file covers whether the numbers on it mean anything.
"""
import time
from unittest import mock

import pytest
from prometheus_client import generate_latest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from pymix.controllers.db_controller import DbController
from pymix.model.db_tables import Base
from pymix.services import metrics


def _value(name: str, **labels) -> float:
    """One sample's value from the live registry, or 0.0 if it does not exist yet."""
    got = metrics.REGISTRY.get_sample_value(name, labels or None)
    return 0.0 if got is None else got


@pytest.fixture
def db_controller():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return DbController(
        session_factory=sessionmaker(bind=engine), app_env="test", max_library_size=0
    )


@pytest.fixture(autouse=True)
def reset_counters():
    """Every metric here is a module-level singleton shared across the file."""
    metrics.invite_request_submissions_total.clear()
    metrics.user_logins_total.clear()
    metrics.user_signups_total.clear()
    metrics.user_requests_total.clear()
    metrics.user_last_request_timestamp_seconds.clear()
    metrics.jobs_completed_total.clear()
    metrics.job_duration_seconds.clear()
    metrics.beets_exec_duration_seconds.clear()
    metrics.dependency_request_duration_seconds.clear()
    with metrics._job_started_lock:
        metrics._job_started_at.clear()
    # `.clear()` drops the pre-created zero children too, so put them back -- their
    # existence before anything happens is itself one of the things under test.
    for outcome in metrics.INVITE_SUBMISSION_OUTCOMES:
        metrics.invite_request_submissions_total.labels(outcome=outcome)
    for outcome in ("created", "resumed", "invalid_credentials", "error"):
        metrics.user_logins_total.labels(outcome=outcome)
    for outcome in ("created", "rejected", "error"):
        metrics.user_signups_total.labels(outcome=outcome)
    yield


# --- The invite funnel -------------------------------------------------------------

def test_every_submission_outcome_exists_at_zero_before_anything_happens():
    """A counter that only appears once it is non-zero cannot be alerted on, and shows
    in Grafana as a gap rather than as 'none yet'."""
    body = generate_latest(metrics.REGISTRY).decode()
    for outcome in metrics.INVITE_SUBMISSION_OUTCOMES:
        assert f'pymix_invite_request_submissions_total{{outcome="{outcome}"}} 0.0' in body


def test_a_resubmission_is_counted_separately_from_a_new_signup(db_controller):
    """The single largest source of drift: the write is an upsert, so a second
    submission is a success that adds no row. Counting it as `created` would make the
    submission count and the table permanently disagree with no way to see why."""
    assert db_controller.create_invite_request(
        email="dj@example.com", dj_software="rekordbox"
    ) == "created"
    assert db_controller.create_invite_request(
        email="DJ@Example.com", dj_software="serato"
    ) == "refreshed"


def test_unknown_outcomes_are_dropped_rather_than_minting_a_series():
    """The bounded label set is the whole cardinality defence on a route the open
    internet can reach."""
    metrics.observe_invite_submission("something-new")

    body = generate_latest(metrics.REGISTRY).decode()
    assert "something-new" not in body


def test_drift_is_readable_as_submissions_minus_created():
    for outcome in ("created", "created", "refreshed", "invalid", "rate_limited"):
        metrics.observe_invite_submission(outcome)

    submissions = sum(
        _value("pymix_invite_request_submissions_total", outcome=outcome)
        for outcome in metrics.INVITE_SUBMISSION_OUTCOMES
    )
    created = _value("pymix_invite_request_submissions_total", outcome="created")

    assert submissions == 5
    # Three people pressed the button and are not new rows in the table.
    assert submissions - created == 3


def test_queue_age_is_absent_while_the_queue_is_empty(db_controller):
    """Absent, not zero: zero reads as 'answered instantly' rather than 'nothing to
    answer', and would sit at the bottom of a graph looking healthy."""
    metrics.register_state_collector(db_controller, max_number_of_users=10, environment="test")
    body = generate_latest(metrics.REGISTRY).decode()

    assert "pymix_invite_request_oldest_new_age_seconds" not in body


def test_queue_age_and_last_submission_appear_once_someone_signs_up(db_controller):
    db_controller.create_invite_request(email="dj@example.com", dj_software="serato")
    metrics.register_state_collector(db_controller, max_number_of_users=10, environment="test")

    body = generate_latest(metrics.REGISTRY).decode()
    assert "pymix_invite_request_oldest_new_age_seconds" in body

    age = _value("pymix_invite_request_oldest_new_age_seconds")
    assert 0 <= age < 60
    last = _value("pymix_invite_request_last_arrival_timestamp_seconds")
    assert abs(last - time.time()) < 60


def test_worked_requests_leave_the_queue_age(db_controller):
    """Only `new` counts. Once a request is invited the wait is over, and leaving it in
    would make the oldest-waiting number grow forever."""
    db_controller.create_invite_request(email="dj@example.com", dj_software="serato")
    timings = db_controller.invite_request_timings()
    assert timings["oldest_new_created_at"] is not None

    with db_controller._session_factory() as session:
        from pymix.model.db_tables import InviteRequestRow
        session.query(InviteRequestRow).update({"status": "invited"})
        session.commit()

    timings = db_controller.invite_request_timings()
    assert timings["oldest_new_created_at"] is None
    # The funnel-went-quiet signal is unaffected -- the request did arrive.
    assert timings["last_created_at"] is not None


# --- Engagement --------------------------------------------------------------------

def test_a_users_activity_is_recorded_against_their_own_name():
    metrics.observe_user_request("alice")
    metrics.observe_user_request("alice")
    metrics.observe_user_request("bob")

    assert _value("pymix_user_requests_total", username="alice") == 2
    assert _value("pymix_user_requests_total", username="bob") == 1
    assert abs(
        _value("pymix_user_last_request_timestamp_seconds", username="alice") - time.time()
    ) < 60


def test_sessions_are_counted(db_controller):
    assert db_controller.count_sessions() == 0


# --- Bottlenecks -------------------------------------------------------------------

def test_a_job_is_timed_from_creation_to_completion():
    metrics.job_started("job-1", "import")
    metrics.job_finished("job-1", result=True)

    assert _value(
        "pymix_jobs_completed_total", job="import", outcome="succeeded"
    ) == 1
    assert _value(
        "pymix_job_duration_seconds_count", job="import", outcome="succeeded"
    ) == 1


def test_a_failed_job_is_timed_too():
    """The duration of a failure is the interesting one: a job that takes ten minutes
    to fail and one that fails immediately are different problems."""
    metrics.job_started("job-2", "export")
    metrics.job_finished("job-2", result=False)

    assert _value("pymix_jobs_completed_total", job="export", outcome="failed") == 1
    assert _value("pymix_job_duration_seconds_count", job="export", outcome="failed") == 1


def test_a_completion_with_no_recorded_start_is_still_counted_but_not_timed():
    """A job that outlived a restart. Losing the completion would understate the
    failure rate; inventing a duration for it would be a lie."""
    metrics.job_finished("never-started", result=False)

    assert _value("pymix_jobs_completed_total", job="unknown", outcome="failed") == 1
    assert _value("pymix_job_duration_seconds_count", job="unknown", outcome="failed") == 0


def test_a_finished_job_stops_being_tracked():
    metrics.job_started("job-3", "import")
    metrics.job_finished("job-3", result=True)

    with metrics._job_started_lock:
        assert "job-3" not in metrics._job_started_at


def test_jobs_that_never_complete_cannot_grow_without_bound():
    """pymix runs against a 768m cgroup limit. An unbounded dict keyed by job id is
    not a diagnostic, it is the next incident."""
    for i in range(metrics._MAX_TRACKED_JOBS + 50):
        metrics.job_started(f"job-{i}", "import")

    with metrics._job_started_lock:
        assert len(metrics._job_started_at) <= metrics._MAX_TRACKED_JOBS


def test_jobs_created_through_the_controller_are_timed(db_controller):
    """The hook is in the controller, not the routers, so every import path gets it --
    the client's upload, both importers, and the watch-directory handler."""
    with mock.patch.object(db_controller, "get_user", return_value={"user_id": "u1"}):
        job_id = db_controller.create_import_job("alice", 10, 0)
        with metrics._job_started_lock:
            assert metrics._job_started_at[job_id][0] == "import"

        db_controller.job_completed(job_id, result=True)

    assert _value(
        "pymix_jobs_completed_total", job="import", outcome="succeeded"
    ) == 1


def test_outbound_calls_are_labelled_by_client_not_by_host():
    """The host carries a per-user port, so labelling by it would mint a series per
    user for information the label set already has elsewhere."""
    metrics.observe_dependency_request("subsonic", "GET", time.monotonic(), ok=True)

    assert _value(
        "pymix_dependency_request_duration_seconds_count",
        dependency="subsonic",
        method="GET",
        outcome="ok",
    ) == 1


def test_a_failed_outbound_call_is_still_timed():
    metrics.observe_dependency_request("navidrome", "POST", time.monotonic(), ok=False)

    assert _value(
        "pymix_dependency_request_duration_seconds_count",
        dependency="navidrome",
        method="POST",
        outcome="error",
    ) == 1


@pytest.mark.parametrize(
    "command,expected",
    [
        (["beet", "list", "-f", "$id"], "list"),
        ("beet stats", "stats"),
        ("beet version", "version"),
        (["beet", "import", "-q", "/path"], "import"),
        # Not every exec is a beets command; the backup copy is a plain cp.
        (["cp", "/config/musiclibrary.blb", "/config/backup.blb"], "cp"),
        # A shape this does not anticipate must not become its own series.
        (["--flag"], "<other>"),
        ([], "<other>"),
    ],
)
def test_beets_commands_collapse_to_the_subcommand(command, expected):
    assert metrics.beets_command_label(command) == expected


# --- Signup outcomes ---------------------------------------------------------------
#
# The route's three refusal paths are easy to conflate, and conflating them is the
# whole point of getting this wrong: "the beta is full" and "container creation fell
# over" are different things to go and look at, and #150 added a third -- a replayed
# invite token -- that would otherwise land in the same bucket as a real fault.

@pytest.mark.anyio
async def test_a_successful_signup_is_counted_as_created():
    from pymix.routers.user import CreateUserRequest, create_user

    orchestrator = mock.AsyncMock()
    orchestrator.create.return_value = "session-1"

    await create_user(
        CreateUserRequest(username="dj", password="pw", email="dj@example.com", token="t"),
        services_orchestrator=orchestrator,
    )

    assert _value("pymix_user_signups_total", outcome="created") == 1


@pytest.mark.anyio
async def test_a_replayed_invite_token_is_rejected_not_an_error():
    """laker-93/pymix#150's refusal. A stranger can reach it, so it must not read as
    pymix breaking -- it is somebody presenting a token that is spent or made up."""
    from pymix.controllers.db_controller import InvalidTokenError
    from pymix.routers.user import CreateUserRequest, create_user

    orchestrator = mock.AsyncMock()
    orchestrator.create.side_effect = InvalidTokenError("signup token is not valid")

    await create_user(
        CreateUserRequest(username="dj", password="pw", email="dj@example.com", token="t"),
        services_orchestrator=orchestrator,
    )

    assert _value("pymix_user_signups_total", outcome="rejected") == 1
    assert _value("pymix_user_signups_total", outcome="error") == 0
    assert _value("pymix_user_signups_total", outcome="created") == 0


@pytest.mark.anyio
async def test_hitting_the_user_cap_is_rejected_not_an_error():
    """`create` returns None rather than raising when the cap is hit."""
    from pymix.routers.user import CreateUserRequest, create_user

    orchestrator = mock.AsyncMock()
    orchestrator.create.return_value = None

    await create_user(
        CreateUserRequest(username="dj", password="pw", email="dj@example.com", token="t"),
        services_orchestrator=orchestrator,
    )

    assert _value("pymix_user_signups_total", outcome="rejected") == 1
    assert _value("pymix_user_signups_total", outcome="error") == 0


@pytest.mark.anyio
async def test_a_broken_signup_is_counted_as_an_error_exactly_once():
    from pymix.routers.user import CreateUserRequest, create_user

    orchestrator = mock.AsyncMock()
    orchestrator.create.side_effect = RuntimeError("docker is down")

    await create_user(
        CreateUserRequest(username="dj", password="pw", email="dj@example.com", token="t"),
        services_orchestrator=orchestrator,
    )

    # Not also `rejected`: the exception leaves session_id at "", not None, so the cap
    # branch must not fire behind it.
    assert _value("pymix_user_signups_total", outcome="error") == 1
    assert _value("pymix_user_signups_total", outcome="rejected") == 0
