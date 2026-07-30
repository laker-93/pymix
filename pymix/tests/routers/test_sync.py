from pathlib import Path
from unittest import mock

import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse

from pymix.routers.sync import sync_download


@pytest.mark.anyio
async def test_sync_download_streams_existing_file(tmp_path):
    downloads_dir = tmp_path / "demoadmin" / "downloads"
    downloads_dir.mkdir(parents=True)
    (downloads_dir / "music.zip").write_bytes(b"zip-bytes")

    fb_file_handler = mock.Mock()
    fb_file_handler.get_downloads_dir = mock.Mock(return_value=downloads_dir)

    response = await sync_download(
        filename="music.zip",
        user={"username": "demoadmin"},
        fb_file_handler=fb_file_handler,
    )

    assert isinstance(response, FileResponse)
    assert Path(response.path) == downloads_dir / "music.zip"
    fb_file_handler.get_downloads_dir.assert_called_once_with("demoadmin")


@pytest.mark.anyio
async def test_sync_download_missing_file_404s(tmp_path):
    downloads_dir = tmp_path / "demoadmin" / "downloads"
    downloads_dir.mkdir(parents=True)

    fb_file_handler = mock.Mock()
    fb_file_handler.get_downloads_dir = mock.Mock(return_value=downloads_dir)

    with pytest.raises(HTTPException) as exc_info:
        await sync_download(
            filename="does_not_exist.zip",
            user={"username": "demoadmin"},
            fb_file_handler=fb_file_handler,
        )

    assert exc_info.value.status_code == 404


@pytest.mark.anyio
async def test_sync_download_missing_downloads_dir_404s(tmp_path):
    # Directory never created (e.g. user has never exported anything yet).
    downloads_dir = tmp_path / "demoadmin" / "downloads"

    fb_file_handler = mock.Mock()
    fb_file_handler.get_downloads_dir = mock.Mock(return_value=downloads_dir)

    with pytest.raises(HTTPException) as exc_info:
        await sync_download(
            filename="music.zip",
            user={"username": "demoadmin"},
            fb_file_handler=fb_file_handler,
        )

    assert exc_info.value.status_code == 404


@pytest.mark.anyio
async def test_sync_download_blocks_path_traversal(tmp_path):
    downloads_dir = tmp_path / "demoadmin" / "downloads"
    downloads_dir.mkdir(parents=True)
    secret = tmp_path / "demoadmin" / "secret.txt"
    secret.write_text("shh")

    fb_file_handler = mock.Mock()
    fb_file_handler.get_downloads_dir = mock.Mock(return_value=downloads_dir)

    with pytest.raises(HTTPException) as exc_info:
        await sync_download(
            filename="../secret.txt",
            user={"username": "demoadmin"},
            fb_file_handler=fb_file_handler,
        )

    assert exc_info.value.status_code == 404


@pytest.mark.anyio
async def test_sync_download_reads_whatever_dir_the_resolved_username_maps_to(tmp_path):
    """require_reader resolves `demo` -> demoadmin's user row *before* this handler
    runs, so `user["username"]` here is already "demoadmin" for a demo session —
    this just confirms the handler looks up the downloads dir for whatever
    username it's handed, which is what makes that upstream proxying effective
    for downloads too (see auth.py require_reader / issue #66)."""
    downloads_dir = tmp_path / "demoadmin" / "downloads"
    downloads_dir.mkdir(parents=True)
    (downloads_dir / "subbox_rb_export.xml").write_text("<xml/>")

    fb_file_handler = mock.Mock()
    fb_file_handler.get_downloads_dir = mock.Mock(return_value=downloads_dir)

    response = await sync_download(
        filename="subbox_rb_export.xml",
        user={"username": "demoadmin"},
        fb_file_handler=fb_file_handler,
    )

    assert Path(response.path) == downloads_dir / "subbox_rb_export.xml"
