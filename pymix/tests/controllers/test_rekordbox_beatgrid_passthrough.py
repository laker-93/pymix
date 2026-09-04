"""A Rekordbox beat grid survives import and export (laker-93/pymix#155, #156).

Phase 2 of docs/design-beatgrids.md: Rekordbox -> subbox -> Rekordbox, using
only pyrekordbox, with no Serato encoder and no cross-format conversion. It
proves the storage shape and both ends of the XML seam before any of the harder
work depends on them.

pyrekordbox's TEMPO write/read cycle was checked before these tests were built
on it -- unlike `Rating`, which writes as "3" and reads back None (#146), TEMPO
nodes round-trip exactly.

The whole point is that the two halves must agree, so these drive the real
import loop into the real export orchestrator rather than asserting against a
hand-written blob in the middle.
"""
from pathlib import Path
from unittest import mock

import pytest
from pyrekordbox.rbxml import RekordboxXml

from pymix.controllers.rekordbox_xml_controller import RekordboxXMLController
from pymix.model.subboxtrack import SubBoxTrack
from pymix.orchestrators.rekordbox_xml_orchestrator import RekordboxXMLOrchestrator

TRACK = dict(Name="Zenith Lantern", Artist="Fixture", Album="QA")


def _xml_with_grid(anchors, bpm=128.0):
    xml = RekordboxXml(name="rekordbox", version="6.0.0", company="AlphaTheta")
    track = xml.add_track("/music/Fixture/QA/zenith.mp3", AverageBpm=bpm, **TRACK)
    for inizio, node_bpm, metro, battito in anchors:
        track.add_tempo(Inizio=inizio, Bpm=node_bpm, Metro=metro, Battito=battito)
    return xml


def _orchestrator(db_controller=None):
    return RekordboxXMLOrchestrator(
        rekordbox_xml_factory=mock.Mock(),
        db_controller=db_controller or mock.Mock(),
        local_user_music_stem="music/{user}",
    )


def _controller(db_controller):
    return RekordboxXMLController(
        subsonic_orchestrator=mock.Mock(),
        rekordbox_xml_orchestrator=_orchestrator(db_controller),
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
    """Run the import metadata pass; return the cuedata blob it stored."""
    matched = SubBoxTrack(
        name=TRACK["Name"], artist=TRACK["Artist"], album=TRACK["Album"],
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
    ):
        await controller._set_metadata_from_xml(
            {"username": "demoadmin"}, xml, matcher=matcher
        )
    return controller._db_controller.update_metadata.call_args.kwargs["cuedata"]


def _export(orchestrator, cuedata):
    """Run the export for one track; return the exported pyrekordbox track."""
    orchestrator._db_controller.get_library_entry = mock.Mock(
        return_value={"cuedata": cuedata} if cuedata is not None else None
    )
    out = RekordboxXml(name="rekordbox", version="6.0.0", company="AlphaTheta")
    playlist = out.add_playlist("QA")
    track = SubBoxTrack(name=TRACK["Name"], artist=TRACK["Artist"], album=TRACK["Album"],
                        path=Path("Fixture/QA/zenith.mp3"))
    with mock.patch(
        "pymix.orchestrators.rekordbox_xml_orchestrator.get_subbox_id", return_value="SBX-1"
    ), mock.patch(
        "pymix.orchestrators.rekordbox_xml_orchestrator.get_duration", return_value=180
    ):
        orchestrator.add_track_to_rekordbox_playlist(
            out, "music/demoadmin", {"username": "demoadmin"}, track, playlist
        )
    return out.get_tracks()[0]


def _anchors(track):
    return [(t.Inizio, t.Bpm, t.Metro, t.Battito) for t in track.tempos]


@pytest.mark.anyio
async def test_a_constant_tempo_grid_makes_the_round_trip():
    db_controller = mock.Mock()
    controller = _controller(db_controller)
    anchors = [(0.046, 128.0, "4/4", 1)]

    cuedata = await _import(controller, _xml_with_grid(anchors))
    assert cuedata["beatgrid"] == [
        {"position_ms": 46, "beats_till_next": None, "bpm": 128.0,
         "metro": "4/4", "battito": 1}
    ]

    assert _anchors(_export(controller._rekordbox_xml_orchestrator, cuedata)) == anchors


@pytest.mark.anyio
async def test_a_variable_tempo_grid_makes_the_round_trip_with_metro_and_battito():
    # The multi-anchor path -- the half of the format with the arithmetic in it,
    # and the one no Serato fixture on the QA machine exercises (design doc §7).
    # Metro and Battito survive here because the grid never touches Serato.
    db_controller = mock.Mock()
    controller = _controller(db_controller)
    anchors = [
        (0.046, 128.0, "4/4", 1),
        (30.5, 140.0, "4/4", 3),
        (60.25, 136.5, "3/4", 2),
    ]

    cuedata = await _import(controller, _xml_with_grid(anchors))
    assert [m["position_ms"] for m in cuedata["beatgrid"]] == [46, 30500, 60250]

    assert _anchors(_export(controller._rekordbox_xml_orchestrator, cuedata)) == anchors


@pytest.mark.anyio
async def test_a_track_with_no_tempo_nodes_stores_no_grid_and_exports_none():
    db_controller = mock.Mock()
    controller = _controller(db_controller)

    cuedata = await _import(controller, _xml_with_grid([]))
    # No key at all, so the row is indistinguishable from one written before
    # grids existed -- and the export emits nothing rather than an empty grid.
    assert "beatgrid" not in cuedata
    assert _export(controller._rekordbox_xml_orchestrator, cuedata).tempos == []


def test_a_row_written_before_grids_existed_exports_no_tempo():
    orchestrator = _orchestrator()
    legacy = {"cues": [{"index": 0, "position": 1000, "name": "in"}], "loops": []}

    exported = _export(orchestrator, legacy)
    assert exported.tempos == []
    # ...and the cues it does carry are untouched by any of this.
    assert [(m.Type, m.Start) for m in exported.marks] == [("cue", 1.0)]


def test_an_unreadable_stored_grid_exports_no_tempo_rather_than_a_wrong_one():
    orchestrator = _orchestrator()
    exported = _export(orchestrator, {"beatgrid": [{"bpm": 128.0}], "cues": [], "loops": []})
    assert exported.tempos == []
