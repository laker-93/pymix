"""The tree routes (#201, #206) through a real app: the guards, the 409 a user
without a tree gets, which the client reads as "render the flat list", and how the
controller's refusals come out over HTTP."""
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pymix.containers import Container
from pymix.controllers.playlist_tree_controller import (
    UNCHANGED, NodeNotFound, PlaylistNotCreated, TreeInvariantError, TreeNotEnabled,
)
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


# --- the write routes (#206) -------------------------------------------------------------

NODE = {'node_id': 'n1', 'parent_id': None, 'position': 0, 'kind': 'folder', 'name': 'House',
        'navidrome_playlist_id': None}


def test_create_a_folder(client, tree):
    tree.create_folder = mock.AsyncMock(return_value=NODE)
    client.cookies.set('session_id', 'dj')

    response = client.post('/playlists/folders', json={'name': 'House', 'parent_id': 'p', 'position': 2})

    assert response.json() == NODE
    tree.create_folder.assert_awaited_once_with({'username': 'dj', 'user_id': 'u'}, 'House', parent_id='p', position=2)


def test_create_a_playlist_in_a_folder(client, tree):
    tree.create_playlist = mock.AsyncMock(return_value=NODE)
    client.cookies.set('session_id', 'dj')

    client.post('/playlists', json={'name': 'Sunday', 'parent_id': 'p', 'song_ids': ['s2', 's1']})

    tree.create_playlist.assert_awaited_once_with(
        {'username': 'dj', 'user_id': 'u'}, 'Sunday', parent_id='p', song_ids=['s2', 's1'])


@pytest.mark.parametrize('body, parent_id', [
    ({'position': 1}, UNCHANGED),              # a reorder: parent_id absent
    ({'parent_id': None, 'position': 1}, None),  # a move to the root
    ({'parent_id': 'p'}, 'p'),
])
def test_patch_tells_an_absent_parent_from_the_root(client, tree, body, parent_id):
    tree.update_node = mock.AsyncMock(return_value=NODE)
    client.cookies.set('session_id', 'dj')

    assert client.patch('/playlists/nodes/n1', json=body).status_code == 200
    assert tree.update_node.await_args.kwargs['parent_id'] is parent_id or \
        tree.update_node.await_args.kwargs['parent_id'] == parent_id


def test_patch_renames(client, tree):
    tree.update_node = mock.AsyncMock(return_value=NODE)
    client.cookies.set('session_id', 'dj')

    client.patch('/playlists/nodes/n1', json={'name': 'House'})

    tree.update_node.assert_awaited_once_with(
        {'username': 'dj', 'user_id': 'u'}, 'n1', name='House', parent_id=UNCHANGED, position=None)


def test_an_empty_patch_is_refused(client, tree):
    tree.update_node = mock.AsyncMock()
    client.cookies.set('session_id', 'dj')

    assert client.patch('/playlists/nodes/n1', json={}).status_code == 400
    tree.update_node.assert_not_called()


@pytest.mark.parametrize('method, path, body', [
    ('post', '/playlists/folders', {'name': 'x', 'position': -1}),
    ('patch', '/playlists/nodes/n1', {'position': -1}),
])
def test_a_negative_position_is_refused(client, tree, method, path, body):
    client.cookies.set('session_id', 'dj')
    assert getattr(client, method)(path, json=body).status_code == 422


WRITES = [
    ('create_folder', 'post', '/playlists/folders', {'name': 'x'}),
    ('create_playlist', 'post', '/playlists', {'name': 'x'}),
    ('update_node', 'patch', '/playlists/nodes/n1', {'name': 'x'}),
]


@pytest.mark.parametrize('method_name, method, path, body', WRITES)
@pytest.mark.parametrize('error, status, detail', [
    (TreeNotEnabled('dj'), 409, 'tree_not_enabled'),
    (NodeNotFound('no live node n1'), 404, 'no live node n1'),
    (TreeInvariantError('cannot move n1 into its own subtree'), 400, 'cannot move n1 into its own subtree'),
    (PlaylistNotCreated('x'), 502, 'Navidrome did not create the playlist'),
])
def test_a_refusal_becomes_its_status(client, tree, method_name, method, path, body, error, status, detail):
    setattr(tree, method_name, mock.AsyncMock(side_effect=error))
    client.cookies.set('session_id', 'dj')

    response = getattr(client, method)(path, json=body)

    assert (response.status_code, response.json()) == (status, {'detail': detail})


@pytest.mark.parametrize('method_name, method, path, body', WRITES)
def test_demo_cannot_write_a_tree(client, tree, method_name, method, path, body):
    setattr(tree, method_name, mock.AsyncMock())
    client.cookies.set('session_id', 'demo')

    assert getattr(client, method)(path, json=body).status_code == 403
    getattr(tree, method_name).assert_not_called()
