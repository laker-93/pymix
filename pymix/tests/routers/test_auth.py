from unittest import mock

import pytest
from fastapi import HTTPException

from pymix.routers.auth import require_reader, require_uploader


def test_require_reader_passes_through_non_demo_user():
    user = {'username': 'demoadmin'}
    db_controller = mock.Mock()

    result = require_reader(user=user, db_controller=db_controller)

    assert result is user
    db_controller.get_user.assert_not_called()


def test_require_reader_proxies_demo_to_demoadmin():
    demoadmin_row = {'username': 'demoadmin'}
    db_controller = mock.Mock()
    db_controller.get_user = mock.Mock(return_value=demoadmin_row)

    result = require_reader(user={'username': 'demo'}, db_controller=db_controller)

    db_controller.get_user.assert_called_once_with('demoadmin')
    assert result is demoadmin_row


def test_require_uploader_passes_through_non_demo_user():
    user = {'username': 'demoadmin'}

    result = require_uploader(user=user)

    assert result is user


def test_require_uploader_blocks_demo():
    with pytest.raises(HTTPException) as exc_info:
        require_uploader(user={'username': 'demo'})

    assert exc_info.value.status_code == 403
