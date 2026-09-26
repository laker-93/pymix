"""
HTTP tests for the trash routes and DELETE /track's trash_batch_id (#200), through a
real app so the paths, the guards and the response shapes are what a client sees.
"""
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pymix.containers import Container
from pymix.routers import auth, track as track_router, trash as trash_router
from pymix.services.trash import PurgeOutcome, TrashDeleteOutcome


def _batch(batch_id, states, kind='track'):
    return {
        'batch_id': batch_id, 'kind': kind, 'label': f'{len(states)} tracks', 'bytes': 100,
        'created_at': 1.0, 'expires_at': 2.0, 'username': 'dj',
        'items': [
            {'id': n, 'subbox_id': f's{n}', 'relative_path': f'A/{n}.mp3', 'size': 50, 'state': s}
            for n, s in enumerate(states)
        ],
    }


@pytest.fixture
def db_controller():
    db = mock.Mock()
    db.get_user_by_session_id.side_effect = lambda session_id: {'username': session_id, 'user_id': 'u'}
    db.trash_bytes.return_value = 100
    return db


@pytest.fixture
def trash_service():
    service = mock.Mock()
    service.purge_batch = mock.AsyncMock(
        side_effect=lambda batch_id, username=None: PurgeOutcome(batch_id=batch_id, n_purged=2)
    )
    return service


@pytest.fixture
def client(db_controller, trash_service):
    container = Container()
    container.db_controller.override(db_controller)
    container.trash_service.override(trash_service)
    container.wire(modules=[auth, trash_router, track_router])
    app = FastAPI()
    app.include_router(trash_router.router)
    app.include_router(track_router.router)
    try:
        yield TestClient(app)
    finally:
        container.unwire()


def _as(client, username):
    client.cookies.set('session_id', username)
    return client


def test_the_trash_lists_only_what_can_still_be_restored(client, db_controller):
    db_controller.get_trash_batches.return_value = [
        _batch('live', ['restorable', 'restorable']),
        _batch('gone', ['purged']),
        _batch('back', ['restored']),
    ]

    body = _as(client, 'dj').get('/trash').json()

    assert [b['batch_id'] for b in body['batches']] == ['live']
    [batch] = body['batches']
    assert batch['label'] == '2 tracks' and batch['deleted_at'] == 1.0 and batch['expires_at'] == 2.0
    assert batch['items'][0] == {'subbox_id': 's0', 'relative_path': 'A/0.mp3', 'size': 50, 'state': 'restorable'}
    assert body['trash_bytes'] == 100


def test_demo_has_no_trash(client):
    assert _as(client, 'demo').get('/trash').status_code == 403
    assert _as(client, 'demo').delete('/trash').status_code == 403


def test_purging_a_batch_that_is_not_yours_is_a_404(client, db_controller, trash_service):
    db_controller.get_trash_batch.return_value = None

    response = _as(client, 'dj').delete('/trash/theirs')

    assert response.status_code == 404
    db_controller.get_trash_batch.assert_called_with('theirs', 'dj')
    trash_service.purge_batch.assert_not_called()


def test_purging_a_batch(client, db_controller, trash_service):
    db_controller.get_trash_batch.return_value = _batch('b1', ['restorable'])

    body = _as(client, 'dj').delete('/trash/b1').json()

    assert body == {'success': True, 'batch_id': 'b1', 'n_purged': 2, 'errors': []}
    trash_service.purge_batch.assert_awaited_once_with('b1', 'dj')


def test_emptying_the_trash_purges_every_live_batch(client, db_controller, trash_service):
    db_controller.get_trash_batches.return_value = [
        _batch('b1', ['restorable']), _batch('b2', ['purged']), _batch('b3', ['expired']),
    ]

    body = _as(client, 'dj').delete('/trash').json()

    assert body == {'success': True, 'n_purged': 4, 'errors': []}
    assert [c.args[0] for c in trash_service.purge_batch.await_args_list] == ['b1', 'b3']


def test_delete_track_names_its_trash_batch_and_commits_only_what_left_beets(
        client, db_controller, trash_service):
    trash_service.trash_tracks = mock.AsyncMock(return_value=TrashDeleteOutcome(
        batch_id='b9', removed={'a', 'ghost'}, not_removed={'stuck': 'track stuck still present in beets'},
    ))

    response = _as(client, 'dj').request('DELETE', '/track', json={'ids': ['a', 'stuck', 'ghost', 'a']})

    body = response.json()
    assert body['trash_batch_id'] == 'b9'
    assert body['success'] is False
    assert body['results'] == [
        {'subbox_id': 'a', 'reason': '', 'success': True},
        {'subbox_id': 'stuck', 'reason': 'track stuck still present in beets', 'success': False},
        {'subbox_id': 'ghost', 'reason': '', 'success': True},
    ]
    trash_service.trash_tracks.assert_awaited_once_with('dj', ['a', 'stuck', 'ghost'])
    assert [c.kwargs['subbox_id'] for c in db_controller.delete_track.call_args_list] == ['a', 'ghost']


def test_a_delete_that_could_not_start_touches_no_rows(client, db_controller, trash_service):
    trash_service.trash_tracks = mock.AsyncMock(side_effect=RuntimeError('beets unreachable'))

    body = _as(client, 'dj').request('DELETE', '/track', json={'ids': ['a']}).json()

    assert body['success'] is False and body['trash_batch_id'] is None
    assert 'beets unreachable' in body['results'][0]['reason']
    db_controller.delete_track.assert_not_called()
