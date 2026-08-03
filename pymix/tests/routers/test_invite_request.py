from unittest import mock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from pymix.routers import invite_request as invite_request_router
from pymix.routers.invite_request import (
    MAX_BODY_BYTES,
    RATE_LIMIT_MAX_REQUESTS,
    check_rate_limit,
    create_invite_request,
    parse_invite_request,
)


def _request(body: bytes = b"", headers: dict = None, client_host: str = "10.0.0.1") -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/invite-request",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": (client_host, 1234),
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


@pytest.fixture(autouse=True)
def clear_rate_limit_state():
    invite_request_router._recent_requests.clear()
    yield
    invite_request_router._recent_requests.clear()


# ── body parsing / validation ──────────────────────────────────────────────

@pytest.mark.anyio
async def test_parses_a_valid_submission():
    body = await parse_invite_request(
        _request(b'{"email": "dj@example.com", "dj_software": "serato"}')
    )

    assert body.email == "dj@example.com"
    assert body.dj_software == "serato"
    assert body.dj_software_other is None


@pytest.mark.anyio
async def test_keeps_free_text_only_for_other():
    body = await parse_invite_request(
        _request(b'{"email": "dj@example.com", "dj_software": "other", "dj_software_other": "Traktor"}')
    )
    assert body.dj_software_other == "Traktor"

    # A client that leaves a stale value in the field must not be able to mislabel a
    # known package, so the free text is dropped when it can't apply.
    body = await parse_invite_request(
        _request(b'{"email": "dj@example.com", "dj_software": "rekordbox", "dj_software_other": "Traktor"}')
    )
    assert body.dj_software_other is None


@pytest.mark.anyio
async def test_invalid_email_is_400_not_422():
    with pytest.raises(HTTPException) as exc_info:
        await parse_invite_request(_request(b'{"email": "not-an-email", "dj_software": "serato"}'))

    assert exc_info.value.status_code == 400


@pytest.mark.anyio
async def test_unknown_dj_software_is_400():
    with pytest.raises(HTTPException) as exc_info:
        await parse_invite_request(
            _request(b'{"email": "dj@example.com", "dj_software": "traktor"}')
        )

    assert exc_info.value.status_code == 400


@pytest.mark.anyio
async def test_malformed_json_is_400():
    with pytest.raises(HTTPException) as exc_info:
        await parse_invite_request(_request(b"not json at all"))

    assert exc_info.value.status_code == 400


@pytest.mark.anyio
async def test_oversized_body_is_rejected_before_parsing():
    oversized = b"x" * (MAX_BODY_BYTES + 1)

    with pytest.raises(HTTPException) as exc_info:
        await parse_invite_request(_request(oversized))
    assert exc_info.value.status_code == 413

    # ...and a lying content-length is caught without reading the body at all.
    with pytest.raises(HTTPException) as exc_info:
        await parse_invite_request(
            _request(b"{}", headers={"content-length": str(MAX_BODY_BYTES + 1)})
        )
    assert exc_info.value.status_code == 413


# ── rate limiting ──────────────────────────────────────────────────────────

def test_rate_limit_allows_then_blocks_one_ip():
    for _ in range(RATE_LIMIT_MAX_REQUESTS):
        check_rate_limit(_request(client_host="10.0.0.1"))

    with pytest.raises(HTTPException) as exc_info:
        check_rate_limit(_request(client_host="10.0.0.1"))

    assert exc_info.value.status_code == 429


def test_rate_limit_is_per_ip():
    for _ in range(RATE_LIMIT_MAX_REQUESTS):
        check_rate_limit(_request(client_host="10.0.0.1"))

    # A different caller is unaffected by the first one exhausting its quota.
    check_rate_limit(_request(client_host="10.0.0.2"))


def test_rate_limit_uses_the_proxy_appended_forwarded_for_entry():
    # Traefik appends the peer it actually saw, so the rightmost entry is the real
    # client; the leftmost is whatever the client chose to send. Spoofing the leftmost
    # must not buy extra quota.
    headers = {"x-forwarded-for": "1.2.3.4, 203.0.113.7"}
    for _ in range(RATE_LIMIT_MAX_REQUESTS):
        check_rate_limit(_request(headers=headers))

    with pytest.raises(HTTPException) as exc_info:
        check_rate_limit(_request(headers={"x-forwarded-for": "9.9.9.9, 203.0.113.7"}))

    assert exc_info.value.status_code == 429


# ── the route itself ───────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_route_persists_and_returns_ok():
    db_controller = mock.Mock()
    body = await parse_invite_request(
        _request(b'{"email": "DJ@Example.com", "dj_software": "other", "dj_software_other": "Traktor"}')
    )

    result = await create_invite_request(body=body, db_controller=db_controller)

    assert result == {"status": "ok"}
    db_controller.create_invite_request.assert_called_once_with(
        email="DJ@example.com",
        dj_software="other",
        dj_software_other="Traktor",
    )
