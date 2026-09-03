"""
BPM must survive a Rekordbox round trip (laker-93/pymix#152).

It used not to, in either direction. Import did `int(track.AverageBpm)` before
writing to beets, so 128.5 became 128; export never wrote `AverageBpm` at all,
so a track imported with a tempo came back out with none and Rekordbox
re-analysed it from scratch.

beets' `bpm` is genuinely a fixed INTEGER field, so the rounded value really is
the best beets can hold -- the exact one is kept in `meta_history.cuedata`,
under the `bpm` key the cuedata schema (routers/track.py) already declared and
nothing wrote. These tests drive the real import loop and the real export
orchestrator against each other, so the two halves cannot drift apart.
"""
from pathlib import Path
from unittest import mock

import pytest
from pyrekordbox.rbxml import RekordboxXml

from pymix.controllers.rekordbox_xml_controller import RekordboxXMLController
from pymix.model.subboxplaylist import SubBoxPlaylist
from pymix.model.subboxtrack import SubBoxTrack
from pymix.orchestrators.rekordbox_xml_orchestrator import RekordboxXMLOrchestrator


def _xml_with_bpm(bpm):
    """A one-track Rekordbox XML, with `AverageBpm` set only if `bpm` is given."""
    xml = RekordboxXml(name="rekordbox", version="6.0.0", company="AlphaTheta")
    kwargs = {"Name": "Zenith Lantern", "Artist": "Fixture", "Album": "QA"}
    if bpm is not None:
        kwargs["AverageBpm"] = bpm
    xml.add_track("/music/Fixture/QA/zenith.mp3", **kwargs)
    return xml


def _controller(db_controller):
    return RekordboxXMLController(
        subsonic_orchestrator=mock.Mock(),
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
    )


async def _import(controller, xml):
    """Run the import metadata pass over `xml`, with everything remote stubbed."""
    matched = SubBoxTrack(
        name="Zenith Lantern", artist="Fixture", album="QA",
        pymix_path=Path("/private-music/Fixture/QA/zenith.mp3"),
    )
    matcher = mock.Mock()
    matcher.match = mock.AsyncMock(return_value=(matched, 1.0))
    controller._subsonic_orchestrator.update_tracks_with_subid = mock.AsyncMock()
    controller._subsonic_orchestrator.set_ratings = mock.AsyncMock()

    with mock.patch(
        "pymix.controllers.rekordbox_xml_controller.get_subbox_id", return_value="SBX-1"
    ), mock.patch.object(Path, "exists", return_value=True), mock.patch.object(
        RekordboxXMLController, "_modify_bpms"
    ) as modify_bpms:
        await controller._set_metadata_from_xml(
            {"username": "demoadmin"}, xml, matcher=matcher
        )
    return modify_bpms


def _export(orchestrator, cuedata, track):
    """Run the export over one track, with `cuedata` standing in for the DB row."""
    orchestrator._db_controller.get_library_entry = mock.Mock(
        return_value={"cuedata": cuedata} if cuedata is not None else None
    )
    xml = RekordboxXml(name="rekordbox", version="6.0.0", company="AlphaTheta")
    playlist = xml.add_playlist("QA")
    with mock.patch(
        "pymix.orchestrators.rekordbox_xml_orchestrator.get_subbox_id", return_value="SBX-1"
    ), mock.patch(
        "pymix.orchestrators.rekordbox_xml_orchestrator.get_duration", return_value=180
    ):
        orchestrator.add_track_to_rekordbox_playlist(
            xml, "music/demoadmin", {"username": "demoadmin"}, track, playlist
        )
    return xml.get_tracks()[0]


@pytest.mark.anyio
async def test_a_fractional_bpm_survives_import_and_export():
    db_controller = mock.Mock()
    controller = _controller(db_controller)

    modify_bpms = await _import(controller, _xml_with_bpm(128.5))

    # beets gets the rounded integer -- that is all its INTEGER field can hold...
    modify_bpms.assert_called_once_with("demoadmin", [("SBX-1", 128)])
    # ...and the exact value is kept in cuedata alongside the cues.
    cuedata = db_controller.update_metadata.call_args.kwargs["cuedata"]
    assert cuedata["bpm"] == 128.5

    # The export reads it back and writes it out unchanged.
    exported = _export(
        controller._rekordbox_xml_orchestrator,
        cuedata,
        SubBoxTrack(name="Zenith Lantern", artist="Fixture", album="QA",
                    path=Path("Fixture/QA/zenith.mp3")),
    )
    assert exported.AverageBpm == 128.5


@pytest.mark.anyio
async def test_bpm_is_rounded_not_truncated_for_beets():
    # The old code did int(), so 128.9 reached beets as 128 -- a whole BPM out,
    # in the direction that always loses.
    db_controller = mock.Mock()
    controller = _controller(db_controller)

    modify_bpms = await _import(controller, _xml_with_bpm(128.9))

    modify_bpms.assert_called_once_with("demoadmin", [("SBX-1", 129)])
    assert db_controller.update_metadata.call_args.kwargs["cuedata"]["bpm"] == 128.9


@pytest.mark.anyio
async def test_a_track_with_no_bpm_in_the_xml_stores_none_and_exports_none():
    db_controller = mock.Mock()
    controller = _controller(db_controller)

    modify_bpms = await _import(controller, _xml_with_bpm(None))

    # No tempo to write anywhere -- and no `bpm` key, so a row written by this
    # code is indistinguishable from every row written before it.
    modify_bpms.assert_called_once_with("demoadmin", [])
    cuedata = db_controller.update_metadata.call_args.kwargs["cuedata"]
    assert "bpm" not in cuedata

    exported = _export(
        controller._rekordbox_xml_orchestrator,
        cuedata,
        SubBoxTrack(name="Zenith Lantern", artist="Fixture", album="QA",
                    path=Path("Fixture/QA/zenith.mp3")),
    )
    assert exported.AverageBpm is None


def test_export_falls_back_to_the_track_bpm_when_cuedata_has_none():
    # A track that never came through a Rekordbox import has no cuedata row; its
    # tempo arrives on SubBoxTrack, read off the beets-written tag by Navidrome.
    orchestrator = RekordboxXMLOrchestrator(
        rekordbox_xml_factory=mock.Mock(),
        db_controller=mock.Mock(),
        local_user_music_stem="music/{user}",
    )
    track = SubBoxTrack(name="Zenith Lantern", artist="Fixture", album="QA",
                        path=Path("Fixture/QA/zenith.mp3"), bpm=174)

    assert _export(orchestrator, None, track).AverageBpm == 174.0
    # ...and cuedata wins where both exist, being the un-rounded one.
    assert _export(orchestrator, {"bpm": 174.3}, track).AverageBpm == 174.3


def test_an_unusable_bpm_does_not_break_the_export():
    # Subsonic servers vary in what they report; a junk value must cost the
    # tempo, not the track.
    orchestrator = RekordboxXMLOrchestrator(
        rekordbox_xml_factory=mock.Mock(),
        db_controller=mock.Mock(),
        local_user_music_stem="music/{user}",
    )
    track = SubBoxTrack(name="Zenith Lantern", artist="Fixture", album="QA",
                        path=Path("Fixture/QA/zenith.mp3"))

    exported = _export(orchestrator, {"bpm": "not a tempo"}, track)
    assert exported.AverageBpm is None
    assert exported.Name == "Zenith Lantern"
