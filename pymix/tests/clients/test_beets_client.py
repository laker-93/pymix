from unittest import mock

import pytest

from pymix.clients.beets_client import BeetsClient
from pymix.clients.beets_exec import BeetsExec


def _make_client(mock_beets_exec, tmp_path):
    return BeetsClient(
        app_env="test",
        beets_exec=mock_beets_exec,
        serving_music_path_base=str(tmp_path),
    )


@pytest.mark.anyio
async def test_get_number_of_tracks_reads_via_beets_exec_without_the_write_lock(tmp_path):
    mock_beets_exec = mock.Mock(spec=BeetsExec)
    mock_beets_exec.execute.return_value = "Tracks: 42\nTotal time: 1:00:00\n"
    beets_client = _make_client(mock_beets_exec, tmp_path)

    count = await beets_client.get_number_of_tracks({"username": "demoadmin"})

    assert count == 42
    mock_beets_exec.execute.assert_called_once_with("beetsdemoadmin", "beet stats")
    mock_beets_exec.write_lock.assert_not_called()


@pytest.mark.anyio
async def test_get_number_of_tracks_public_uses_shared_container(tmp_path):
    mock_beets_exec = mock.Mock(spec=BeetsExec)
    mock_beets_exec.execute.return_value = "Tracks: 7\n"
    beets_client = _make_client(mock_beets_exec, tmp_path)

    count = await beets_client.get_number_of_tracks({"username": "demoadmin"}, public=True)

    assert count == 7
    mock_beets_exec.execute.assert_called_once_with("beets", "beet stats")


@pytest.mark.anyio
async def test_count_tracks_on_disk_counts_audio_files_without_a_beets_exec(tmp_path):
    mock_beets_exec = mock.Mock(spec=BeetsExec)
    beets_client = _make_client(mock_beets_exec, tmp_path)

    user_dir = tmp_path / "demoadmin" / "Artist" / "Album"
    user_dir.mkdir(parents=True)
    (user_dir / "track1.mp3").write_bytes(b"")
    (user_dir / "track2.flac").write_bytes(b"")
    (user_dir / "cover.jpg").write_bytes(b"")  # non-audio, must not count

    count = await beets_client.count_tracks_on_disk({"username": "demoadmin"})

    assert count == 2
    mock_beets_exec.execute.assert_not_called()


@pytest.mark.anyio
async def test_count_tracks_on_disk_is_scoped_to_the_users_own_directory(tmp_path):
    mock_beets_exec = mock.Mock(spec=BeetsExec)
    beets_client = _make_client(mock_beets_exec, tmp_path)

    (tmp_path / "demoadmin").mkdir()
    (tmp_path / "demoadmin" / "track1.mp3").write_bytes(b"")
    other_user_dir = tmp_path / "otheruser"
    other_user_dir.mkdir()
    (other_user_dir / "track1.mp3").write_bytes(b"")
    (other_user_dir / "track2.mp3").write_bytes(b"")

    count = await beets_client.count_tracks_on_disk({"username": "demoadmin"})

    assert count == 1


@pytest.mark.anyio
async def test_count_tracks_on_disk_returns_zero_before_any_import(tmp_path):
    mock_beets_exec = mock.Mock(spec=BeetsExec)
    beets_client = _make_client(mock_beets_exec, tmp_path)

    count = await beets_client.count_tracks_on_disk({"username": "brand-new-user"})

    assert count == 0
    mock_beets_exec.execute.assert_not_called()
