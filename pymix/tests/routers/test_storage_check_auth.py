"""subbox-app#202 — storage_check must 401 a caller it can't identify.

It used to answer 200 with `allowed: False` and a 0 MB quota when the session cookie
was missing, which the client read as "storage full". The client's reauth interceptor
keys off 401, so it never refreshed the cookie: after an app restart dropped the
session, the first upload stopped on "Storage Limit Reached — 0 MB / 0 MB".
"""
from unittest import mock

import pytest
from fastapi import HTTPException

from pymix.routers.user import storage_check


def _db(user=None, lookup_error=None):
    db_controller = mock.Mock()
    if lookup_error:
        db_controller.get_user_by_session_id = mock.Mock(side_effect=lookup_error)
    else:
        db_controller.get_user_by_session_id = mock.Mock(return_value=user)
    db_controller.user_library_size_exceeded = mock.Mock(return_value=(False, 1000, 400))
    db_controller.trash_bytes = mock.Mock(return_value=100)
    return db_controller


@pytest.mark.anyio
async def test_no_session_is_a_401_not_a_full_quota():
    with pytest.raises(HTTPException) as exc_info:
        await storage_check(uploadSizeBytes=0, session_id=None, authorization=None, db_controller=_db())

    assert exc_info.value.status_code == 401


@pytest.mark.anyio
async def test_an_unknown_session_is_a_401():
    # This one used to answer allowed=True with success=False.
    with pytest.raises(HTTPException) as exc_info:
        await storage_check(uploadSizeBytes=0, session_id='stale', authorization=None, db_controller=_db(user=None))

    assert exc_info.value.status_code == 401


@pytest.mark.anyio
async def test_a_failed_session_lookup_is_a_401():
    with pytest.raises(HTTPException) as exc_info:
        await storage_check(
            uploadSizeBytes=0, session_id='dup', authorization=None,
            db_controller=_db(lookup_error=RuntimeError('two sessions')),
        )

    assert exc_info.value.status_code == 401


@pytest.mark.anyio
@pytest.mark.parametrize('cookie, authorization', [('abc', None), (None, 'Bearer abc')])
async def test_an_identified_caller_gets_their_quota(cookie, authorization):
    db_controller = _db(user={'username': 'alice'})

    result = await storage_check(
        uploadSizeBytes=0, session_id=cookie, authorization=authorization, db_controller=db_controller,
    )

    db_controller.get_user_by_session_id.assert_called_once_with('abc')
    assert result['success'] is True
    assert result['allowed'] is True
    assert result['maxStorageBytes'] == 1000
    assert result['remainingBytes'] == 600
    assert result['libraryBytes'] == 300
