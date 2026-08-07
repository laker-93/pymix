import pytest

from pymix.clients.beets_client import BeetsClient


def _make_client(tmp_path):
    return BeetsClient(
        app_env="test",
        serving_music_path_base=str(tmp_path),
    )


@pytest.mark.anyio
async def test_count_tracks_on_disk_counts_audio_files(tmp_path):
    beets_client = _make_client(tmp_path)

    user_dir = tmp_path / "demoadmin" / "Artist" / "Album"
    user_dir.mkdir(parents=True)
    (user_dir / "track1.mp3").write_bytes(b"")
    (user_dir / "track2.flac").write_bytes(b"")
    (user_dir / "cover.jpg").write_bytes(b"")  # non-audio, must not count

    count = await beets_client.count_tracks_on_disk({"username": "demoadmin"})

    assert count == 2


@pytest.mark.anyio
async def test_count_tracks_on_disk_is_scoped_to_the_users_own_directory(tmp_path):
    beets_client = _make_client(tmp_path)

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
    beets_client = _make_client(tmp_path)

    count = await beets_client.count_tracks_on_disk({"username": "brand-new-user"})

    assert count == 0


@pytest.mark.anyio
async def test_count_tracks_on_disk_public_reads_the_shared_library_directory(tmp_path):
    beets_client = _make_client(tmp_path)

    public_dir = tmp_path / "public"
    public_dir.mkdir()
    (public_dir / "track1.mp3").write_bytes(b"")
    (tmp_path / "demoadmin").mkdir()
    (tmp_path / "demoadmin" / "track1.mp3").write_bytes(b"")

    count = await beets_client.count_tracks_on_disk({"username": "demoadmin"}, public=True)

    assert count == 1
