from unittest import mock

import pytest

from pymix.clients.beets_client import BeetsClient
from pymix.clients.beets_exec import BeetsExec


@pytest.mark.anyio
async def test_get_number_of_tracks_reads_via_beets_exec_without_the_write_lock():
    mock_beets_exec = mock.Mock(spec=BeetsExec)
    mock_beets_exec.execute.return_value = "Tracks: 42\nTotal time: 1:00:00\n"
    beets_client = BeetsClient(app_env="test", beets_exec=mock_beets_exec)

    count = await beets_client.get_number_of_tracks({"username": "demoadmin"})

    assert count == 42
    mock_beets_exec.execute.assert_called_once_with("beetsdemoadmin", "beet stats")
    mock_beets_exec.write_lock.assert_not_called()


@pytest.mark.anyio
async def test_get_number_of_tracks_public_uses_shared_container():
    mock_beets_exec = mock.Mock(spec=BeetsExec)
    mock_beets_exec.execute.return_value = "Tracks: 7\n"
    beets_client = BeetsClient(app_env="test", beets_exec=mock_beets_exec)

    count = await beets_client.get_number_of_tracks({"username": "demoadmin"}, public=True)

    assert count == 7
    mock_beets_exec.execute.assert_called_once_with("beets", "beet stats")
