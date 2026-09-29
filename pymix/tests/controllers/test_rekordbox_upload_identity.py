"""
An uploaded track is found by its file, not its name (laker-93/pymix#239).

A Rekordbox upload used to find every XML track in Navidrome by title, artist
and album. A file whose own tags differ from its Rekordbox name -- an untagged
WAV is "Untagged Wav 06.wav" by [Unknown Artist] in Navidrome -- matched
nothing, so the audio imported but the track landed in no playlist and lost its
cues, loops, bpm and rating. Measured on prod on 2026-09-29: 10 of 127.

/sync/map_meta records each uploaded file's path on the user's machine against
the subbox_id it tagged the server's copy with, so the import can find the
track by the XML's Location instead. These tests drive the real import passes
with a name match that finds nothing, as it did on prod.
"""
from unittest import mock

import pytest
from pyrekordbox.rbxml import RekordboxXml

from pymix.controllers.rekordbox_xml_controller import RekordboxXMLController
from pymix.orchestrators.rekordbox_xml_orchestrator import RekordboxXMLOrchestrator
from pymix.orchestrators.subsonic_orchestrator import SubsonicOrchestrator
from pymix.routers.sync import Track, Tracks, match_tracks
from pymix.utils.rekordbox_location import user_location_from_xml

USER = {"username": "demoadmin", "password": "p"}
WAV_LOCATION = "/Users/dj/Music/Untagged Wav 06.wav"
MP3_LOCATION = "/Users/dj/Music/Known.mp3"


def _xml():
    """Two tracks in one playlist: an uploaded WAV whose Rekordbox name its file
    tags don't carry, and an MP3 the library already had."""
    xml = RekordboxXml(name="rekordbox", version="6.0.0", company="AlphaTheta")
    wav = xml.add_track(WAV_LOCATION, Name="Opener", Artist="Real Artist", Album="Set", AverageBpm=124.0, Rating=204)  # 4 stars
    mp3 = xml.add_track(MP3_LOCATION, Name="Known", Artist="Someone", Album="LP", AverageBpm=128.0)
    # What Rekordbox itself writes. pyrekordbox's encode_path would give
    # file://localhost//Users/..., also handled (see test_user_location_*).
    wav._element.set("Location", "file://localhost/Users/dj/Music/Untagged%20Wav%2006.wav")
    mp3._element.set("Location", "file://localhost/Users/dj/Music/Known.mp3")
    playlist = xml.add_playlist("Set 1")
    playlist.add_track(wav.TrackID)
    playlist.add_track(mp3.TrackID)
    return xml


def _controller(db_controller, native_client):
    orchestrator = SubsonicOrchestrator(mock.Mock(), native_client=native_client)
    orchestrator.set_ratings = mock.AsyncMock()
    playlist_tree = mock.Mock()
    playlist_tree.import_playlists = mock.AsyncMock(return_value=None)
    return RekordboxXMLController(
        subsonic_orchestrator=orchestrator,
        rekordbox_xml_orchestrator=RekordboxXMLOrchestrator(
            rekordbox_xml_factory=mock.Mock(),
            db_controller=db_controller,
            local_user_music_stem="music/{user}",
        ),
        rb_backup_file_handler=mock.Mock(),
        file_browser_file_handler=mock.Mock(),
        subsonic_client=mock.Mock(),
        db_controller=db_controller,
        wishlist_reconcile_service=mock.Mock(),
        restored_db_output_root="foo",
        local_user_music_stem="music/{user}",
        serving_music_path_base="/private-music",
        beets_exec=mock.Mock(),
        playlist_tree_controller=playlist_tree,
    )


def _native(rows):
    native = mock.Mock()
    native.songs_by_subbox_id = mock.AsyncMock(return_value=rows)
    return native


def _row(song_id, subbox_id, path, created, missing=False):
    return {"id": song_id, "path": path, "missing": missing, "createdAt": created,
            "tags": {"subboxid": [subbox_id]}}


async def _import(controller, known_match=None):
    """Run the import's playlist and metadata passes. The name match finds only
    `known_match`, and only for the MP3."""
    matcher = mock.Mock()

    async def match(user, title, artist, album=None):
        return (known_match, 1.0) if known_match and title == "Known" else None

    def titles():
        return [c.kwargs.get("title", c.args[1] if len(c.args) > 1 else None) for c in matcher.match.call_args_list]
    matcher.titles = titles
    matcher.match = mock.AsyncMock(side_effect=match)
    with mock.patch("pymix.controllers.rekordbox_xml_controller.TrackMatcher", return_value=matcher), \
            mock.patch.object(RekordboxXMLController, "_modify_bpms", return_value={}) as modify_bpms:
        await controller._set_data_from_xml(USER, _xml())
    return matcher, modify_bpms


@pytest.mark.anyio
async def test_an_uploaded_file_whose_tags_differ_from_its_name_lands_in_its_playlist_with_its_metadata():
    db_controller = mock.Mock()
    db_controller.get_library_ids_by_user_location.return_value = {WAV_LOCATION: "SBX-WAV", MP3_LOCATION: None}
    controller = _controller(db_controller, _native([_row("nd-wav", "SBX-WAV", "a/Untagged Wav 06.wav", "2026-09-29")]))

    matcher, modify_bpms = await _import(controller)

    # The playlist gets the uploaded file, by its Navidrome id...
    playlists = controller._playlist_tree.import_playlists.call_args.args[1]
    wav, mp3 = playlists[0].tracks
    assert wav.sub_track_id == "nd-wav"
    # ...and the track nothing could find stays unmatched, as before.
    assert mp3.sub_track_id is None
    # Its cues/bpm/grid are written against its subbox_id, and its bpm reaches beets.
    written = [c.kwargs["subbox_id"] for c in db_controller.update_metadata.call_args_list]
    assert written == ["SBX-WAV"]
    modify_bpms.assert_called_once_with("demoadmin", [("SBX-WAV", 124)])
    # Its rating goes to the same Navidrome track.
    rated = controller._subsonic_orchestrator.set_ratings.call_args.args[1]
    assert [(t.sub_track_id, t.rating) for t in rated] == [("nd-wav", 4)]
    # And nothing searched Navidrome by the name its file doesn't carry.
    assert "Opener" not in matcher.titles()


@pytest.mark.anyio
async def test_a_track_the_upload_did_not_send_is_still_matched_by_name():
    from pathlib import Path
    from pymix.model.subboxtrack import SubBoxTrack

    db_controller = mock.Mock()
    db_controller.get_library_ids_by_user_location.return_value = {WAV_LOCATION: None, MP3_LOCATION: None}
    controller = _controller(db_controller, _native([]))
    known = SubBoxTrack(name="Known", artist="Someone", album="LP", sub_track_id="nd-mp3",
                        pymix_path=Path("/private-music/demoadmin/Known.mp3"))

    with mock.patch("pymix.controllers.rekordbox_xml_controller.get_subbox_id", return_value="SBX-MP3"), \
            mock.patch.object(Path, "exists", return_value=True):
        await _import(controller, known_match=known)

    wav, mp3 = controller._playlist_tree.import_playlists.call_args.args[1][0].tracks
    assert (wav.sub_track_id, mp3.sub_track_id) == (None, "nd-mp3")
    assert [c.kwargs["subbox_id"] for c in db_controller.update_metadata.call_args_list] == ["SBX-MP3"]


@pytest.mark.anyio
async def test_an_uploaded_file_navidrome_has_no_tag_for_still_gets_its_metadata():
    """No row with the subboxid tag (a Navidrome without the tag configured): the
    playlist entry falls back to the name match, the metadata still lands by id."""
    db_controller = mock.Mock()
    db_controller.get_library_ids_by_user_location.return_value = {WAV_LOCATION: "SBX-WAV"}
    controller = _controller(db_controller, _native([]))

    await _import(controller)

    wav, _ = controller._playlist_tree.import_playlists.call_args.args[1][0].tracks
    assert wav.sub_track_id is None
    assert [c.kwargs["subbox_id"] for c in db_controller.update_metadata.call_args_list] == ["SBX-WAV"]


@pytest.mark.anyio
async def test_a_failed_lookup_falls_back_to_matching_by_name():
    db_controller = mock.Mock()
    db_controller.get_library_ids_by_user_location.side_effect = RuntimeError("db down")
    controller = _controller(db_controller, _native([]))

    matcher, _ = await _import(controller)

    # The import goes on exactly as before #239: every track is looked up by name.
    assert "Opener" in matcher.titles()
    db_controller.update_metadata.assert_not_called()


@pytest.mark.anyio
async def test_song_ids_by_subbox_id_takes_the_oldest_live_row():
    """A retried upload leaves `foo.1.wav` carrying the same tag; the original is
    the one playlists already point at. A missing (trashed) row never counts."""
    orchestrator = SubsonicOrchestrator(mock.Mock(), native_client=_native([
        _row("nd-dup", "SBX-1", "a/foo.1.wav", "2026-09-29T12:00:00Z"),
        _row("nd-gone", "SBX-1", "a/foo.wav", "2026-09-01T00:00:00Z", missing=True),
        _row("nd-orig", "SBX-1", "a/foo.wav", "2026-09-29T11:00:00Z"),
        _row("nd-other", "SBX-OTHER", "a/other.wav", "2026-09-29T11:00:00Z"),
    ]))

    assert await orchestrator.song_ids_by_subbox_id(USER, {"SBX-1"}) == {"SBX-1": "nd-orig"}


@pytest.mark.anyio
async def test_song_ids_by_subbox_id_is_best_effort():
    native = mock.Mock()
    native.songs_by_subbox_id = mock.AsyncMock(side_effect=RuntimeError("navidrome down"))

    assert await SubsonicOrchestrator(mock.Mock(), native_client=native).song_ids_by_subbox_id(USER, {"SBX-1"}) == {}
    assert await SubsonicOrchestrator(mock.Mock()).song_ids_by_subbox_id(USER, {"SBX-1"}) == {}


@pytest.mark.parametrize("location, expected", [
    # What Rekordbox writes on macOS.
    ("file://localhost/Users/dj/Music/Untagged%20Wav%2006.wav", "/Users/dj/Music/Untagged Wav 06.wav"),
    # What pyrekordbox (and so subbox's own export) writes.
    ("file://localhost//Users/dj/My%20Track%20%26%20Co.mp3", "/Users/dj/My Track & Co.mp3"),
    # Windows: no slash before the drive, backslashes, as path.win32.resolve.
    ("file://localhost/C:/Users/DJ/Music/Caf%C3%A9/a%20b.flac", "C:\\Users\\DJ\\Music\\Café\\a b.flac"),
    ("file://localhost/Volumes/USB/./x/../y.mp3", "/Volumes/USB/y.mp3"),
    ("", None),
    (None, None),
])
def test_user_location_is_spelt_as_the_client_spells_it(location, expected):
    """Each expected value is what subbox-app's parseTrack gives for the same
    Location (checked against it with node), so the lookup keys agree."""
    assert user_location_from_xml(location) == expected


# --- /sync/match_tracks (the retry) --------------------------------------------------

def _no_name_match_client():
    client = mock.Mock()
    client.library_is_empty = mock.AsyncMock(return_value=False)
    client.get_track_match = mock.AsyncMock(return_value=None)
    return client


@pytest.mark.anyio
async def test_match_tracks_does_not_ask_for_a_file_an_earlier_upload_already_sent():
    """The retry: the name match can't find the WAV, so it was uploaded again and
    beets kept both copies (`Untagged Wav 06.1.wav`)."""
    db_controller = mock.Mock()
    db_controller.get_library_ids_by_user_location.return_value = {
        WAV_LOCATION: "SBX-WAV", "/Users/dj/Music/New.wav": None,
    }
    client = _no_name_match_client()

    response = await match_tracks(
        tracks=Tracks(tracks=[
            Track(title="Opener", artist="Real Artist", userLocation=WAV_LOCATION),
            Track(title="New", artist="Someone", userLocation="/Users/dj/Music/New.wav"),
        ]),
        user=USER, subsonic_client=client, db_controller=db_controller,
    )

    assert [(t.title, t.matched) for t in response.tracks] == [("Opener", True), ("New", False)]
    # The already-uploaded file never cost a Navidrome search.
    assert [c.args[1] for c in client.get_track_match.call_args_list] == ["New"]


@pytest.mark.anyio
async def test_match_tracks_without_locations_does_not_touch_the_db():
    """An older client sends no userLocation: the endpoint behaves as before."""
    db_controller = mock.Mock()
    await match_tracks(
        tracks=Tracks(tracks=[Track(title="Opener", artist="Real Artist")]),
        user=USER, subsonic_client=_no_name_match_client(), db_controller=db_controller,
    )
    db_controller.get_library_ids_by_user_location.assert_not_called()
