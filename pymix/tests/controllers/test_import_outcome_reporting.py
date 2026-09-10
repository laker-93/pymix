"""
Fault injection per import phase: a phase that breaks must not produce a green
job (laker-93/pymix#171).

This is pymix#135's own repro without the `docker stop`. The bug was reproduced
on 2026-08-23 by stopping the user's beets container mid-import: all eight bpm
writes raised inside `_modify_bpms`, every one was caught and logged, nothing
propagated to `run_import_task`, and the job row came back result=true. The
tests below break each phase's dependency the same way and assert the verdict
the ledger computes -- so the next loop that swallows its failures fails a test
instead of a user's import.

Uses a real BeetsExec (so write_lock genuinely works) with the underlying
docker.execute call patched, same pattern as test_rekordbox_xml_controller_batched_writes.py.
"""
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from pymix.clients.beets_exec import BeetsExec
from pymix.controllers.rekordbox_xml_controller import RekordboxXMLController
from pymix.services.import_progress import ImportProgressReporter
from pymix.services.job_outcome import Verdict


def _make_controller(beets_exec, db_controller=None, subsonic_orchestrator=None,
                     rekordbox_xml_orchestrator=None):
    return RekordboxXMLController(
        subsonic_orchestrator=subsonic_orchestrator or mock.AsyncMock(),
        rekordbox_xml_orchestrator=rekordbox_xml_orchestrator or mock.Mock(),
        rb_backup_file_handler=mock.Mock(),
        file_browser_file_handler=mock.Mock(),
        subsonic_client=mock.Mock(),
        db_controller=db_controller or mock.Mock(),
        wishlist_reconcile_service=mock.Mock(),
        restored_db_output_root="foo",
        local_user_music_stem="foo",
        serving_music_path_base="/private-music",
        beets_exec=beets_exec,
    )


def _reporter():
    """A real reporter over a mock DbController, so the ledger is the real one."""
    return ImportProgressReporter(mock.Mock(), "job-1")


def _xml_track(track_id, name):
    return SimpleNamespace(
        TrackID=track_id,
        Name=name,
        Artist="An Artist",
        Album="An Album",
        AverageBpm=128.0,
        marks=[],
        tempos=[],
        rating=0,
    )


def _matcher_matching(paths_by_name):
    """A TrackMatcher stand-in resolving each track name to a pymix path."""
    matcher = mock.AsyncMock()

    async def match(user, title, artist, album=None):
        path = paths_by_name.get(title)
        if path is None:
            return None
        return [SimpleNamespace(pymix_path=path, sub_track_id=f"nd-{title}")]

    matcher.match.side_effect = match
    return matcher


def _rekordbox_xml(tracks):
    xml = mock.Mock()
    xml.get_tracks.return_value = tracks
    return xml


# ---------------------------------------------------------------- applying_metadata


@pytest.mark.anyio
async def test_a_metadata_phase_whose_every_bpm_write_failed_is_a_failed_job(tmp_path):
    # pymix#135: the user's beets container is down, so every write in
    # _modify_bpms raises and is caught. Nothing escapes run_import_task.
    audio = tmp_path / "one.mp3"
    audio.write_bytes(b"")
    tracks = [_xml_track(1, "Track One"), _xml_track(2, "Track Two")]
    beets_exec = BeetsExec()
    orchestrator = mock.Mock()
    orchestrator.get_all_xml_tracks.return_value = []
    controller = _make_controller(beets_exec, rekordbox_xml_orchestrator=orchestrator)
    progress = _reporter()

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker, \
         mock.patch("pymix.controllers.rekordbox_xml_controller.get_subbox_id") as get_subbox_id:
        mock_docker.execute.side_effect = RuntimeError("container beetsdemo is not running")
        get_subbox_id.side_effect = lambda p: f"SBX-{p.name}"

        await controller._set_metadata_from_xml(
            {"username": "demo"},
            _rekordbox_xml(tracks),
            progress=progress,
            matcher=_matcher_matching({"Track One": audio, "Track Two": audio}),
        )

    outcome = progress.verdict()

    assert outcome.verdict is Verdict.FAILURE
    assert outcome.result is False
    assert "0 of 2" in outcome.reason


@pytest.mark.anyio
async def test_a_metadata_phase_that_matched_nothing_is_a_failed_job(tmp_path):
    # The other way this phase does no work: Navidrome has none of the XML's
    # tracks, so every one is skipped. Nothing raises here at all.
    tracks = [_xml_track(1, "Track One"), _xml_track(2, "Track Two")]
    orchestrator = mock.Mock()
    orchestrator.get_all_xml_tracks.return_value = []
    controller = _make_controller(BeetsExec(), rekordbox_xml_orchestrator=orchestrator)
    progress = _reporter()

    with mock.patch("pymix.clients.beets_exec.docker"):
        await controller._set_metadata_from_xml(
            {"username": "demo"},
            _rekordbox_xml(tracks),
            progress=progress,
            matcher=_matcher_matching({}),
        )

    outcome = progress.verdict()

    assert outcome.verdict is Verdict.FAILURE
    assert "no matching track in your library" in outcome.reason


@pytest.mark.anyio
async def test_a_metadata_phase_that_missed_one_track_is_a_partial_not_a_failure(tmp_path):
    # Most of the work landed. The user gets a green screen that says what did
    # not happen, rather than a red one -- the Serato precedent, generalised.
    audio = tmp_path / "one.mp3"
    audio.write_bytes(b"")
    tracks = [_xml_track(1, "Track One"), _xml_track(2, "Track Two")]
    orchestrator = mock.Mock()
    orchestrator.get_all_xml_tracks.return_value = []
    controller = _make_controller(BeetsExec(), rekordbox_xml_orchestrator=orchestrator)
    progress = _reporter()

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker, \
         mock.patch("pymix.controllers.rekordbox_xml_controller.get_subbox_id") as get_subbox_id:
        mock_docker.execute.return_value = "APPLIED 1 MISSING 0\n"
        get_subbox_id.side_effect = lambda p: "SBX-1"

        await controller._set_metadata_from_xml(
            {"username": "demo"},
            _rekordbox_xml(tracks),
            progress=progress,
            matcher=_matcher_matching({"Track One": audio}),
        )

    outcome = progress.verdict()

    assert outcome.verdict is Verdict.PARTIAL
    assert outcome.result is True
    assert "1 skipped" in outcome.warnings


@pytest.mark.anyio
async def test_a_metadata_phase_that_updated_everything_is_a_clean_success(tmp_path):
    audio = tmp_path / "one.mp3"
    audio.write_bytes(b"")
    tracks = [_xml_track(1, "Track One")]
    orchestrator = mock.Mock()
    orchestrator.get_all_xml_tracks.return_value = []
    controller = _make_controller(BeetsExec(), rekordbox_xml_orchestrator=orchestrator)
    progress = _reporter()

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker, \
         mock.patch("pymix.controllers.rekordbox_xml_controller.get_subbox_id") as get_subbox_id:
        mock_docker.execute.return_value = "APPLIED 1 MISSING 0\n"
        get_subbox_id.side_effect = lambda p: "SBX-1"

        await controller._set_metadata_from_xml(
            {"username": "demo"},
            _rekordbox_xml(tracks),
            progress=progress,
            matcher=_matcher_matching({"Track One": audio}),
        )

    outcome = progress.verdict()

    assert outcome.verdict is Verdict.SUCCESS
    assert not outcome.warnings


@pytest.mark.anyio
async def test_a_bpm_the_batch_matched_no_beets_item_for_is_recorded_as_a_failure(tmp_path):
    # The batched write ran fine and reached none of its tracks. It reported that
    # on stdout and we used to log it as a warning and move on.
    audio = tmp_path / "one.mp3"
    audio.write_bytes(b"")
    tracks = [_xml_track(1, "Track One")]
    orchestrator = mock.Mock()
    orchestrator.get_all_xml_tracks.return_value = []
    controller = _make_controller(BeetsExec(), rekordbox_xml_orchestrator=orchestrator)
    progress = _reporter()

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker, \
         mock.patch("pymix.controllers.rekordbox_xml_controller.get_subbox_id") as get_subbox_id:
        mock_docker.execute.return_value = "MISSING SBX-1\nAPPLIED 0 MISSING 1\n"
        get_subbox_id.side_effect = lambda p: "SBX-1"

        await controller._set_metadata_from_xml(
            {"username": "demo"},
            _rekordbox_xml(tracks),
            progress=progress,
            matcher=_matcher_matching({"Track One": audio}),
        )

    outcome = progress.verdict()

    assert outcome.verdict is Verdict.FAILURE
    assert "beets matched no track with this subbox_id" in outcome.reason


# -------------------------------------------------------------------- mapping_ids


def _fake_list(entries, batch_result):
    def fake_execute(container_name, command, stream=False):
        cmd = command if isinstance(command, list) else command.split()
        if cmd[:2] == ["beet", "list"]:
            return iter([("stdout", e.encode()) for e in entries])
        if cmd[:2] == ["python3", "-c"]:
            if isinstance(batch_result, Exception):
                raise batch_result
            return batch_result
        return iter([])
    return fake_execute


def test_a_mapping_phase_where_no_file_carries_its_tag_is_a_failed_job():
    # Every imported file came back without a SUBBOX_ID tag, so nothing could be
    # mapped. Two silent `continue`s used to make this indistinguishable from a
    # clean run.
    beets_exec = BeetsExec()
    controller = _make_controller(beets_exec)
    progress = _reporter()

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker, \
         mock.patch("pymix.controllers.rekordbox_xml_controller.get_subbox_id") as get_subbox_id, \
         mock.patch.object(Path, "exists", return_value=True):
        mock_docker.execute.side_effect = _fake_list(
            ["1:/music/a/one.mp3", "2:/music/a/two.mp3"], "APPLIED 0 MISSING 0\n"
        )
        get_subbox_id.return_value = None

        controller._map_subbox_id_beet_id("demo", public=False, progress=progress)

    outcome = progress.verdict()

    assert outcome.verdict is Verdict.FAILURE
    assert "no SUBBOX_ID tag" in outcome.reason


def test_a_mapping_phase_that_mapped_every_track_is_a_clean_success():
    beets_exec = BeetsExec()
    controller = _make_controller(beets_exec)
    progress = _reporter()

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker, \
         mock.patch("pymix.controllers.rekordbox_xml_controller.get_subbox_id") as get_subbox_id, \
         mock.patch.object(Path, "exists", return_value=True):
        mock_docker.execute.side_effect = _fake_list(
            ["1:/music/a/one.mp3", "2:/music/a/two.mp3"], "APPLIED 2 MISSING 0\n"
        )
        get_subbox_id.side_effect = ["SBX-1", "SBX-2"]

        controller._map_subbox_id_beet_id("demo", public=False, progress=progress)

    assert progress.verdict().verdict is Verdict.SUCCESS


def test_a_mapping_phase_with_nothing_left_to_map_is_still_a_success():
    # A re-import of an already-mapped library: `beet list` returns nothing, so
    # the phase has zero items. Nothing to do is not a failure to do it.
    beets_exec = BeetsExec()
    controller = _make_controller(beets_exec)
    progress = _reporter()

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker:
        mock_docker.execute.side_effect = _fake_list([], "APPLIED 0 MISSING 0\n")

        controller._map_subbox_id_beet_id("demo", public=False, progress=progress)

    assert progress.verdict().verdict is Verdict.SUCCESS


def test_the_subbox_id_write_missing_its_beets_item_is_recorded_as_a_failure():
    # The tag read fine and the write reached nothing. This is the root of the
    # chain _modify_bpms' own comment describes: a track beets does not know the
    # subbox_id of is a track whose later bpm write cannot match anything.
    beets_exec = BeetsExec()
    controller = _make_controller(beets_exec)
    progress = _reporter()

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker, \
         mock.patch("pymix.controllers.rekordbox_xml_controller.get_subbox_id") as get_subbox_id, \
         mock.patch.object(Path, "exists", return_value=True):
        mock_docker.execute.side_effect = _fake_list(
            ["1:/music/a/one.mp3", "2:/music/a/two.mp3"],
            "MISSING 1\nAPPLIED 1 MISSING 1\n",
        )
        get_subbox_id.side_effect = ["SBX-1", "SBX-2"]

        controller._map_subbox_id_beet_id("demo", public=False, progress=progress)

    outcome = progress.verdict()

    assert outcome.verdict is Verdict.PARTIAL
    assert "1 failed" in outcome.warnings
