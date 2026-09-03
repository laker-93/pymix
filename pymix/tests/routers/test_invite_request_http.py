"""End-to-end HTTP tests for `POST /invite-request`.

The unit tests alongside this file call the router's functions directly, which proves the
logic but not the wiring — that the path is where the client expects, that the
`Depends(parse_invite_request)` body really does turn a bad payload into 400 rather than
FastAPI's 422, and that the rate-limit dependency runs at all. Those are exactly the
things subbox-app#69 submits against, so they get a real request through a real app.
"""

from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from prometheus_client import generate_latest

from pymix.containers import Container
from pymix.routers import invite_request as invite_request_router
from pymix.services import metrics


@pytest.fixture
def db_controller():
    controller = mock.Mock()
    # The real controller returns which of the upsert's two branches it took, and the
    # route counts that. A bare Mock would hand the counter a Mock object.
    controller.create_invite_request.return_value = "created"
    return controller


def _submissions(outcome: str) -> float:
    got = metrics.REGISTRY.get_sample_value(
        "pymix_invite_request_submissions_total", {"outcome": outcome}
    )
    return 0.0 if got is None else got


@pytest.fixture
def client(db_controller):
    container = Container()
    container.db_controller.override(db_controller)
    container.wire(modules=[invite_request_router])

    app = FastAPI()
    app.include_router(invite_request_router.router)

    invite_request_router._recent_requests.clear()
    metrics.invite_request_submissions_total.clear()
    for outcome in metrics.INVITE_SUBMISSION_OUTCOMES:
        metrics.invite_request_submissions_total.labels(outcome=outcome)
    try:
        yield TestClient(app)
    finally:
        invite_request_router._recent_requests.clear()
        container.unwire()


def test_accepts_a_submission_with_no_session_cookie(client, db_controller):
    response = client.post(
        "/invite-request",
        json={"email": "dj@example.com", "dj_software": "rekordbox"},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    db_controller.create_invite_request.assert_called_once_with(
        email="dj@example.com", dj_software="rekordbox", dj_software_other=None
    )
    assert "session_id" not in client.cookies


def test_invalid_email_is_400(client, db_controller):
    response = client.post(
        "/invite-request",
        json={"email": "nope", "dj_software": "rekordbox"},
    )

    assert response.status_code == 400
    db_controller.create_invite_request.assert_not_called()


def test_unknown_dj_software_is_400(client, db_controller):
    response = client.post(
        "/invite-request",
        json={"email": "dj@example.com", "dj_software": "traktor"},
    )

    assert response.status_code == 400
    db_controller.create_invite_request.assert_not_called()


def test_missing_field_is_400_not_422(client):
    """FastAPI's own default here is 422; the client renders a 400 inline against the
    email field, so the dependency must be the thing that rejects the body."""
    response = client.post("/invite-request", json={"dj_software": "rekordbox"})

    assert response.status_code == 400


def test_oversized_body_is_413(client, db_controller):
    response = client.post(
        "/invite-request",
        content=b"x" * (invite_request_router.MAX_BODY_BYTES + 1),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413
    db_controller.create_invite_request.assert_not_called()


def test_repeat_submissions_are_rate_limited(client):
    body = {"email": "dj@example.com", "dj_software": "serato"}

    for _ in range(invite_request_router.RATE_LIMIT_MAX_REQUESTS):
        assert client.post("/invite-request", json=body).status_code == 200

    assert client.post("/invite-request", json=body).status_code == 429


def test_response_is_identical_for_a_repeat_address(client):
    """No membership oracle: a caller must not be able to tell from the response whether
    an address was already on the list."""
    body = {"email": "dj@example.com", "dj_software": "serato"}

    first = client.post("/invite-request", json=body)
    second = client.post("/invite-request", json=body)

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()


# --- Funnel instrumentation --------------------------------------------------------
#
# The point of these: every one of the rejections above is a person who pressed the
# button and did not end up in the table. Before this, all of them were indistinguishable
# from never having visited — the route logged and moved on, and the only surviving
# trace of the funnel was the row count, which by definition excludes them.

def test_a_successful_submission_is_counted_as_created(client, db_controller):
    client.post("/invite-request", json={"email": "dj@example.com", "dj_software": "serato"})

    assert _submissions("created") == 1


def test_a_repeat_address_is_counted_as_refreshed_not_created(client, db_controller):
    """The response is deliberately identical either way (see the oracle test above),
    so this counter is the only place the difference survives."""
    db_controller.create_invite_request.return_value = "refreshed"

    client.post("/invite-request", json={"email": "dj@example.com", "dj_software": "serato"})

    assert _submissions("refreshed") == 1
    assert _submissions("created") == 0


def test_a_rejected_body_is_counted_as_invalid(client):
    client.post("/invite-request", json={"email": "nope", "dj_software": "rekordbox"})
    client.post("/invite-request", json={"dj_software": "rekordbox"})

    assert _submissions("invalid") == 2
    assert _submissions("created") == 0


def test_an_oversized_body_is_counted_as_too_large(client):
    client.post(
        "/invite-request",
        content=b"x" * (invite_request_router.MAX_BODY_BYTES + 1),
        headers={"content-type": "application/json"},
    )

    assert _submissions("too_large") == 1


def test_a_rate_limited_submission_is_counted(client):
    body = {"email": "dj@example.com", "dj_software": "serato"}
    for _ in range(invite_request_router.RATE_LIMIT_MAX_REQUESTS + 1):
        client.post("/invite-request", json=body)

    assert _submissions("rate_limited") == 1
    assert _submissions("created") == invite_request_router.RATE_LIMIT_MAX_REQUESTS


def test_a_failed_write_is_counted_and_still_500s(client, db_controller):
    """Counted, not swallowed. A submission lost here is a person lost, so it has to be
    the loudest outcome in the set -- but the caller still gets a real error."""
    db_controller.create_invite_request.side_effect = RuntimeError("db gone")

    with pytest.raises(RuntimeError):
        client.post("/invite-request", json={"email": "dj@example.com", "dj_software": "serato"})

    assert _submissions("error") == 1
    assert _submissions("created") == 0


def test_every_outcome_is_exposed_at_zero_before_anything_happens(client):
    body = generate_latest(metrics.REGISTRY).decode()

    for outcome in metrics.INVITE_SUBMISSION_OUTCOMES:
        assert f'pymix_invite_request_submissions_total{{outcome="{outcome}"}}' in body
