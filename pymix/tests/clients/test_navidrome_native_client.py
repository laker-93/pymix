"""
The native-API client (#200). Navidrome rate-limits logins to a handful per 20s,
so the token must be reused, and refreshed only when it has expired.
"""
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse

import pytest
from aiohttp import ClientResponseError

from pymix.clients.navidrome_native_client import NavidromeNativeClient

USER = {'username': 'dj', 'password': 'pw'}


def _client():
    client = NavidromeNativeClient("http://navidrome{user}:{port}", MagicMock())
    client.post = AsyncMock(side_effect=[{'token': 't1'}, {'token': 't2'}])
    return client


def _expired():
    return ClientResponseError(MagicMock(), (), status=401, message='expired')


@pytest.mark.anyio
async def test_it_logs_in_once_and_reuses_the_token():
    client = _client()
    client.get = AsyncMock(return_value=[])

    await client.list_missing(USER)
    await client.list_missing(USER)

    client.post.assert_awaited_once_with(
        'http://navidromedj:4533/auth/login', json={'username': 'dj', 'password': 'pw'}
    )
    for call in client.get.await_args_list:
        assert call.kwargs['headers'] == {'x-nd-authorization': 'Bearer t1'}


@pytest.mark.anyio
async def test_an_expired_token_is_replaced_once():
    client = _client()
    client.get = AsyncMock(side_effect=[_expired(), []])

    assert await client.list_missing(USER) == []

    assert client.post.await_count == 2
    assert client.get.await_args_list[1].kwargs['headers'] == {'x-nd-authorization': 'Bearer t2'}


@pytest.mark.anyio
async def test_other_errors_are_not_retried():
    client = _client()
    client.delete = AsyncMock(side_effect=ClientResponseError(MagicMock(), (), status=500, message='x'))

    with pytest.raises(ClientResponseError):
        await client.delete_missing(USER, ['a'])

    assert client.post.await_count == 1


@pytest.mark.anyio
async def test_ids_are_sent_in_bounded_chunks():
    client = _client()
    client.delete = AsyncMock(return_value={})
    ids = [f'id{i}' for i in range(120)]

    await client.delete_missing(USER, ids + ids[:5])

    sent = [parse_qs(urlparse(call.args[0]).query)['id'] for call in client.delete.await_args_list]
    assert [len(chunk) for chunk in sent] == [50, 50, 20]
    assert sum(sent, []) == ids


@pytest.mark.anyio
async def test_songs_are_found_by_their_subboxid_tag():
    client = _client()
    client.get = AsyncMock(return_value=[{'id': 'mf'}])

    assert await client.songs_by_subbox_id(USER, ['a', 'b']) == [{'id': 'mf'}]

    query = parse_qs(urlparse(client.get.await_args.args[0]).query)
    assert query['subboxid'] == ['a', 'b']
    # No missing filter: a trashed track's row is missing, and must still be found.
    assert 'missing' not in query


@pytest.mark.anyio
async def test_missing_rows_are_paged():
    client = _client()
    client.get = AsyncMock(side_effect=[[{'id': i} for i in range(500)], [{'id': 'last'}]])

    rows = await client.list_missing(USER)

    assert len(rows) == 501
    second = parse_qs(urlparse(client.get.await_args_list[1].args[0]).query)
    assert second['_start'] == ['500'] and second['_end'] == ['1000']
