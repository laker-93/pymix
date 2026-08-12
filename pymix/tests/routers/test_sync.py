from pathlib import Path
from unittest import mock

import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse

from pymix.routers.sync import SyncPlaylistsRequest, sync_download, sync_playlists


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
async def test_sync_download_is_never_cacheable(tmp_path):
    """Every user's download is the same url, so a cache in front of pymix would
    serve one user's zip to the next caller (Cloudflare caches `.zip` by extension)."""
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

    cache_control = response.headers["cache-control"]
    assert "no-store" in cache_control
    assert "private" in cache_control


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


# ── /sync/playlists: what ends up in the one file the client downloads ─────────
#
# The client gets a single download because a browser only reliably saves one file
# per user gesture — a second one is dropped with no error at all. These
# cover the three shapes of that file, and that a failed XML fails the whole sync
# rather than handing back a zip that's silently missing it.


def _request(**overrides) -> SyncPlaylistsRequest:
    body = {
        "direction": "download",
        "localTracks": [],
        "playlists": [{"id": "pl-1", "source": "subbox"}],
    }
    body.update(overrides)
    return SyncPlaylistsRequest(**body)


def _fb_handler(tmp_path, zip_name="music"):
    """A handler whose sync() records its call and returns a zip path like the real
    one does — the downloads dir path *without* the .zip suffix."""
    downloads_dir = tmp_path / "demoadmin" / "downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)

    fb_file_handler = mock.Mock()
    fb_file_handler.get_xml_output_path = mock.Mock(
        return_value=downloads_dir / "subbox_rb_export.xml"
    )
    fb_file_handler.sync = mock.Mock(return_value=(3, downloads_dir / zip_name))
    return fb_file_handler


@pytest.mark.anyio
async def test_sync_playlists_puts_the_xml_in_the_zip(tmp_path):
    fb_file_handler = _fb_handler(tmp_path)
    xml_controller = mock.AsyncMock()

    result = await sync_playlists(
        request=_request(includeRekordboxXml=True, user_root="/Users/dj/Music"),
        user={"username": "demoadmin"},
        fb_file_handler=fb_file_handler,
        subsonic_client=mock.AsyncMock(),
        rekordbox_xml_controller=xml_controller,
    )

    assert result["success"] is True
    assert result["xmlIncluded"] is True
    # One download, and the client is told its name rather than assembling it.
    assert result["downloadFilename"] == "music.zip"

    xml_path = fb_file_handler.get_xml_output_path.return_value
    assert fb_file_handler.sync.call_args.kwargs["extra_files"] == [
        (xml_path, "subbox_rb_export.xml")
    ]
    # Built for the caller's chosen extraction dir, scoped to the requested playlists.
    assert xml_controller.create_rekordbox_xml_from_subsonic_playlists.await_args.kwargs == {
        "user_root": "/Users/dj/Music",
        "user": {"username": "demoadmin"},
        "xml_path": None,
        "xml_output_path": xml_path,
        "playlist_ids": ["pl-1"],
    }


@pytest.mark.anyio
async def test_sync_playlists_without_xml_zips_tracks_only(tmp_path):
    """The default, and what a client from before this change sends."""
    fb_file_handler = _fb_handler(tmp_path)
    xml_controller = mock.AsyncMock()

    result = await sync_playlists(
        request=_request(),
        user={"username": "demoadmin"},
        fb_file_handler=fb_file_handler,
        subsonic_client=mock.AsyncMock(),
        rekordbox_xml_controller=xml_controller,
    )

    assert result["success"] is True
    assert result["xmlIncluded"] is False
    assert result["downloadFilename"] == "music.zip"
    assert fb_file_handler.sync.call_args.kwargs["extra_files"] is None
    xml_controller.create_rekordbox_xml_from_subsonic_playlists.assert_not_awaited()
    fb_file_handler.get_xml_output_path.assert_not_called()


@pytest.mark.anyio
async def test_sync_playlists_metadata_only_serves_the_xml_and_builds_no_zip(tmp_path):
    fb_file_handler = _fb_handler(tmp_path)
    subsonic_client = mock.AsyncMock()

    result = await sync_playlists(
        request=_request(includeTracks=False, includeRekordboxXml=True),
        user={"username": "demoadmin"},
        fb_file_handler=fb_file_handler,
        subsonic_client=subsonic_client,
        rekordbox_xml_controller=mock.AsyncMock(),
    )

    assert result["success"] is True
    assert result["downloadFilename"] == "subbox_rb_export.xml"
    assert result["zipPath"] is None
    assert result["nTracksExported"] == 0
    fb_file_handler.sync.assert_not_called()
    # None of the local-track matching is needed for an XML, so it's skipped
    # entirely rather than paying for every playlist's tracks.
    subsonic_client.get_playlist_tracks.assert_not_awaited()


@pytest.mark.anyio
async def test_sync_playlists_rejects_a_download_with_nothing_in_it(tmp_path):
    with pytest.raises(HTTPException) as exc_info:
        await sync_playlists(
            request=_request(includeTracks=False, includeRekordboxXml=False),
            user={"username": "demoadmin"},
            fb_file_handler=_fb_handler(tmp_path),
            subsonic_client=mock.AsyncMock(),
            rekordbox_xml_controller=mock.AsyncMock(),
        )

    assert exc_info.value.status_code == 400


@pytest.mark.anyio
async def test_sync_playlists_fails_the_sync_when_the_xml_cannot_be_built(tmp_path):
    """A zip of tracks with no XML in it is indistinguishable from a successful
    tracks-only export, so the whole sync reports failure instead."""
    fb_file_handler = _fb_handler(tmp_path)
    xml_controller = mock.AsyncMock()
    xml_controller.create_rekordbox_xml_from_subsonic_playlists.side_effect = RuntimeError(
        "boom"
    )

    result = await sync_playlists(
        request=_request(includeRekordboxXml=True),
        user={"username": "demoadmin"},
        fb_file_handler=fb_file_handler,
        subsonic_client=mock.AsyncMock(),
        rekordbox_xml_controller=xml_controller,
    )

    assert result["success"] is False
    assert "boom" in result["reason"]
    assert result["downloadFilename"] is None
    assert result["xmlIncluded"] is False
    fb_file_handler.sync.assert_not_called()
