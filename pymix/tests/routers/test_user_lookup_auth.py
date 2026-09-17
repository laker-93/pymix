"""GHSA-hqhc-vv93-fhcv — the two operator lookup helpers must not answer strangers.

`GET /user/get_by_username` and `GET /user/get_by_session_id` resolve a row by a name
or a session id supplied in the request, so neither can identify its caller. Ungated,
they handed any unauthenticated caller who guessed a username the full `user_table`
row — password included, and the same password opens pymix, that user's Navidrome and
filebrowser.

Two independent defences are asserted here, because either alone is one mistake away
from the disclosure coming back:

  1. the routes are gated by `require_admin_token`;
  2. the password is stripped from the payload regardless of who is asking.
"""
from unittest import mock

import pytest
from fastapi import HTTPException

from pymix.routers.admin import require_admin_token
from pymix.routers.user import _without_secrets, get_user, get_user_by_session_id
from pymix.registration import create_app


ROW = {
    'username': 'victim',
    'password': 'hunter2',
    'email': 'victim@example.com',
    'subsonic_port': 34739,
}


# --- 1. the gate ------------------------------------------------------------


def _route(app, path):
    return next(r for r in app.routes if getattr(r, 'path', None) == path)


@pytest.mark.parametrize('path', ['/user/get_by_username', '/user/get_by_session_id'])
def test_the_lookup_routes_declare_the_admin_gate(path):
    # Asserted on the route's own dependency list rather than through a request, so
    # this fails if somebody removes the dependency, not merely if a request 401s for
    # some unrelated reason.
    app = create_app(mock.MagicMock())
    dependants = _route(app, path).dependant.dependencies
    assert any(d.call is require_admin_token for d in dependants), (
        f'{path} is not gated by require_admin_token'
    )


def test_an_unset_admin_token_refuses_rather_than_allows(monkeypatch):
    # Fail closed. An unconfigured deployment must not read as "no auth required",
    # which is exactly how these two came to be open in the first place.
    monkeypatch.delenv('PYMIX_ADMIN_TOKEN', raising=False)

    with pytest.raises(HTTPException) as exc_info:
        require_admin_token(x_admin_token='anything')

    assert exc_info.value.status_code == 503


# --- 2. the payload ---------------------------------------------------------


def test_without_secrets_drops_the_password_and_keeps_everything_else():
    redacted = _without_secrets(ROW)

    assert 'password' not in redacted
    assert redacted == {k: v for k, v in ROW.items() if k != 'password'}


def test_without_secrets_tolerates_a_missing_user():
    # Both handlers start with `user = {}` and leave it that way on a lookup miss.
    assert _without_secrets(None) == {}
    assert _without_secrets({}) == {}


@pytest.mark.anyio
async def test_get_by_username_never_returns_the_password():
    db_controller = mock.Mock()
    db_controller.get_user = mock.Mock(return_value=dict(ROW))

    result = await get_user(username='victim', db_controller=db_controller)

    assert result['success'] is True
    assert 'password' not in result['user']
    assert result['user']['username'] == 'victim'


@pytest.mark.anyio
async def test_get_by_session_id_never_returns_the_password():
    db_controller = mock.Mock()
    db_controller.get_user_by_session_id = mock.Mock(return_value=dict(ROW))

    result = await get_user_by_session_id(session_id='abc123', db_controller=db_controller)

    assert result['success'] is True
    assert 'password' not in result['user']
    assert result['user']['username'] == 'victim'


@pytest.mark.anyio
async def test_the_session_lookup_does_not_log_the_row(caplog):
    # The handler used to log `f'found user {user}'` — an f-string of the whole dict,
    # which wrote the cleartext password into the application log on every call.
    db_controller = mock.Mock()
    db_controller.get_user_by_session_id = mock.Mock(return_value=dict(ROW))

    with caplog.at_level('INFO'):
        await get_user_by_session_id(session_id='abc123', db_controller=db_controller)

    assert 'hunter2' not in caplog.text
    assert 'victim' in caplog.text
