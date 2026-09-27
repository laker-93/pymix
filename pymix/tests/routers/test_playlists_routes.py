"""GET /playlists/tree (#201) through a real app: the guards and the 409 a user
without a tree gets, which the client reads as "render the flat list"."""
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pymix.containers import Container
from pymix.controllers.playlist_tree_controller import TreeNotEnabled
from pymix.routers import auth, playlists


@pytest.fixture
def tree():
    return mock.Mock()


@pytest.fixture
def client(tree):
    db = mock.Mock()
    db.get_user_by_session_id.side_effect = lambda session_id: {'username': session_id, 'user_id': 'u'}
    container = Container()
    container.db_controller.override(db)
    container.playlist_tree_controller.override(tree)
    container.wire(modules=[auth, playlists])
    app = FastAPI()
    app.include_router(playlists.router)
    try:
        yield TestClient(app)
    finally:
        container.unwire()


def test_the_tree(client, tree):
    body = {'nodes': [{'node_id': 'n1', 'parent_id': None, 'position': 0, 'kind': 'folder', 'name': 'House',
                       'navidrome_playlist_id': None, 'child_count': 0}], 'hidden_playlist_ids': ['pl-9']}
    tree.get_tree = mock.AsyncMock(return_value=body)
    client.cookies.set('session_id', 'dj')

    assert client.get('/playlists/tree').json() == body


def test_a_user_without_a_tree_gets_a_409_tree_not_enabled(client, tree):
    tree.get_tree = mock.AsyncMock(side_effect=TreeNotEnabled('dj'))
    client.cookies.set('session_id', 'dj')

    response = client.get('/playlists/tree')

    assert response.status_code == 409 and response.json() == {'detail': 'tree_not_enabled'}


def test_demo_has_no_tree(client, tree):
    tree.get_tree = mock.AsyncMock()
    client.cookies.set('session_id', 'demo')

    assert client.get('/playlists/tree').status_code == 403
    tree.get_tree.assert_not_called()
