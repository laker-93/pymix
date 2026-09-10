from pathlib import Path
from unittest import mock

import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse

from pymix.clients.subsonic_client import SubsonicClient
from pymix.model.subboxtrack import SubBoxTrack
from pymix.routers.sync import (
    SyncPlaylistsRequest,
    Track,
    Tracks,
    match_tracks,
    sync_download,
    sync_playlists,
)


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
    fb_file_handler.get_name_in_export_zip = mock.Mock(
        side_effect=lambda path: f"music/{path.name}"
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
    # Under music/, not beside it — the zip must have a single top-level entry or
    # macOS wraps it in a folder the XML's Locations don't account for.
    assert fb_file_handler.sync.call_args.kwargs["extra_files"] == [
        (xml_path, "music/subbox_rb_export.xml")
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


# --- /sync/match_tracks false positives (#164) --------------------------------------

def _matching_client(library):
    """A real SubsonicClient whose three Navidrome searches answer from ``library``.

    Only the queries are faked: the tier logic and the scoring under test are the real
    ones, so these tests exercise the same path the prod failure took.
    """
    client = SubsonicClient(mock.MagicMock(), mock.MagicMock(), "v", "foo", "bar", None, "test")
    client.library_is_empty = mock.AsyncMock(return_value=False)
    # Tier 1 queries title+artist, tiers 2 and 3 query a title or a single token. The
    # prod library returned nothing for the first two and only surfaced the wrong track
    # once the token tier fanned out, so model exactly that.
    client.query_tracks_by = mock.AsyncMock(return_value=[])
    client.query_track_by_name = mock.AsyncMock(
        side_effect=lambda user, term: library if " " not in term else []
    )
    return client


@pytest.mark.anyio
@pytest.mark.parametrize("xml_title, xml_artist, xml_album, server_track", [
    # A different artist entirely, rescued by a marginal core title and an exact album.
    ("Umbra Anchor 5150-003", "Dune Signal", "Paper Machines",
     ("Tessellate Anchor 904-002", "Aurora Static", "Paper Machines")),
    # The same artist, a different song — the pair the score alone cannot separate.
    ("Tessellate Vantage 5150-002", "Ember Lattice", "Salt Air Sessions",
     ("Zenith Vantage 904-001", "Ember Lattice", "Tidal Notation")),
])
async def test_match_tracks_does_not_claim_an_unrelated_track_is_already_owned(
    xml_title, xml_artist, xml_album, server_track
):
    """#164: a false `matched` here means the file is never uploaded *and* the playlist
    is built from someone else's track, with the job still reporting success."""
    name, artist, album = server_track
    client = _matching_client([SubBoxTrack(name=name, artist=artist, album=album)])

    response = await match_tracks(
        tracks=Tracks(tracks=[Track(title=xml_title, artist=xml_artist, album=xml_album)]),
        user={"username": "demoadmin", "password": "p"},
        subsonic_client=client,
    )

    assert response.tracks[0].matched is False
    # ...and the response echoes what the client asked for, so it uploads that track.
    assert response.tracks[0].title == xml_title


@pytest.mark.anyio
async def test_match_tracks_still_dedups_a_track_the_user_really_has():
    """The stricter bar must not turn every re-upload into a duplicate: ordinary tag
    drift (a track-number prefix, an artist typo, a "(Deluxe)" album) still matches."""
    client = _matching_client([])
    client.query_tracks_by = mock.AsyncMock(return_value=[
        SubBoxTrack(name="One For Vertigo", artist="Skee Mask", album="Compro (Deluxe)"),
    ])

    response = await match_tracks(
        tracks=Tracks(tracks=[
            Track(title="06 One For Vertigo", artist="Skee Msak", album="Compro"),
        ]),
        user={"username": "demoadmin", "password": "p"},
        subsonic_client=client,
    )

    assert response.tracks[0].matched is True
    assert response.tracks[0].title == "One For Vertigo"
