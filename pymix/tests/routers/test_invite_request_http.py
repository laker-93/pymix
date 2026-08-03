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

from pymix.containers import Container
from pymix.routers import invite_request as invite_request_router


@pytest.fixture
def db_controller():
    return mock.Mock()


@pytest.fixture
def client(db_controller):
    container = Container()
    container.db_controller.override(db_controller)
    container.wire(modules=[invite_request_router])

    app = FastAPI()
    app.include_router(invite_request_router.router)

    invite_request_router._recent_requests.clear()
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
