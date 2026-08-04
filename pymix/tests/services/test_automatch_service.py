"""
Tests for the Phase 2 background automatch sweep (laker-93/pymix#79): the idle
test, chunked+yielding reimport, the `pending -> matched | nomatch | error` state
machine (including the error-retry cap), and the single end-of-batch Navidrome
rescan.

Uses a real BeetsExec (so write_lock genuinely serializes, same pattern as
test_beets_exec.py / test_services_orchestrator_beets_migration.py) with the
underlying docker.execute call patched, plus mocked db_controller / subsonic_client
/ rekordbox_xml_controller collaborators.
"""
from unittest import mock

import pytest

from pymix.clients.beets_exec import BeetsExec
from pymix.services.automatch_service import AutomatchService

USER = {"username": "demoadmin", "password": "pw"}


def _make_service(
    beets_exec,
    db_controller=None,
    subsonic_client=None,
    rekordbox_xml_controller=None,
    chunk_size=10,
    wall_clock_budget_s=300,
    idle_recency_window_s=600,
    error_retry_cap=5,
):
    if db_controller is None:
        db_controller = mock.Mock()
        db_controller.get_number_of_jobs.return_value = 0
    if subsonic_client is None:
        subsonic_client = mock.AsyncMock()
        subsonic_client.get_now_playing.return_value = []
    rekordbox_xml_controller = rekordbox_xml_controller or mock.Mock()
    service = AutomatchService(
        db_controller=db_controller,
        beets_exec=beets_exec,
        subsonic_client=subsonic_client,
        rekordbox_xml_controller=rekordbox_xml_controller,
        chunk_size=chunk_size,
        wall_clock_budget_s=wall_clock_budget_s,
        idle_recency_window_s=idle_recency_window_s,
        error_retry_cap=error_retry_cap,
    )
    return service, db_controller, subsonic_client, rekordbox_xml_controller


# --- idle test -----------------------------------------------------------------

@pytest.mark.anyio
async def test_sweep_user_skips_when_import_job_in_progress():
    db_controller = mock.Mock()
    db_controller.get_number_of_jobs.return_value = 1
    subsonic_client = mock.AsyncMock()
    service, *_ = _make_service(BeetsExec(), db_controller=db_controller, subsonic_client=subsonic_client)

    result = await service.sweep_user(USER)

    assert result.skipped is True
    subsonic_client.get_now_playing.assert_not_called()


@pytest.mark.anyio
async def test_sweep_user_skips_when_navidrome_reports_recent_play():
    subsonic_client = mock.AsyncMock()
    subsonic_client.get_now_playing.return_value = [{"minutesAgo": 2}]
    service, *_ = _make_service(BeetsExec(), subsonic_client=subsonic_client, idle_recency_window_s=600)

    result = await service.sweep_user(USER)

    assert result.skipped is True


@pytest.mark.anyio
async def test_sweep_user_treats_play_outside_the_recency_window_as_idle():
    subsonic_client = mock.AsyncMock()
    subsonic_client.get_now_playing.return_value = [{"minutesAgo": 30}]
    beets_exec = BeetsExec()
    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker:
        mock_docker.execute.return_value = ""
        service, *_ = _make_service(beets_exec, subsonic_client=subsonic_client, idle_recency_window_s=600)
        result = await service.sweep_user(USER)

    assert result.skipped is False
    assert result.candidates == 0


@pytest.mark.anyio
async def test_sweep_user_treats_now_playing_check_failure_as_not_idle():
    subsonic_client = mock.AsyncMock()
    subsonic_client.get_now_playing.side_effect = Exception("navidrome unreachable")
    service, *_ = _make_service(BeetsExec(), subsonic_client=subsonic_client)

    result = await service.sweep_user(USER)

    assert result.skipped is True


@pytest.mark.anyio
async def test_sweep_user_treats_in_progress_job_check_failure_as_not_idle():
    db_controller = mock.Mock()
    db_controller.get_number_of_jobs.side_effect = Exception("db unreachable")
    service, *_ = _make_service(BeetsExec(), db_controller=db_controller)

    result = await service.sweep_user(USER)

    assert result.skipped is True


# --- selection + state machine --------------------------------------------------

@pytest.mark.anyio
async def test_sweep_user_selects_pending_and_under_cap_error_tracks_and_restamps_state():
    rekordbox_xml_controller = mock.Mock()
    beets_exec = BeetsExec()

    def fake_execute(container_name, command, stream=False):
        cmd = command if isinstance(command, list) else command.split()
        if cmd[:4] == ["beet", "list", "-f", "$id"] and cmd[4:] == ["automatch_state:pending"]:
            return "1\n2\n"
        if cmd[:4] == ["beet", "list", "-f", "$id:$automatch_attempts"]:
            # id 3 is under the cap (2 < 5) and eligible for retry; id 4 is at the
            # cap and must be excluded.
            return "3:2\n4:5\n"
        if cmd[:2] == ["beet", "-c"]:
            return "import ok"
        if cmd[:4] == ["beet", "list", "-f", "$id:$mb_trackid"]:
            return "1:mbid-1\n2:\n3:mbid-3\n"
        return ""

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker:
        mock_docker.execute.side_effect = fake_execute
        service, db_controller, subsonic_client, rekordbox_xml_controller = _make_service(
            beets_exec, rekordbox_xml_controller=rekordbox_xml_controller, chunk_size=10, error_retry_cap=5
        )
        result = await service.sweep_user(USER)

    assert result.candidates == 3  # ids 1, 2 (pending) + 3 (error, under cap) -- 4 excluded
    assert result.chunks_processed == 1
    rekordbox_xml_controller.remap_subbox_id_for_ids.assert_called_once_with("demoadmin", [1, 2, 3], public=False)
    rekordbox_xml_controller.retag_duplicates.assert_called_once_with("demoadmin", public=False)
    subsonic_client.scan.assert_awaited_once_with(USER)

    modify_calls = [
        call.args[1] for call in mock_docker.execute.call_args_list
        if isinstance(call.args[1], list) and call.args[1][:2] == ["beet", "modify"]
    ]
    stamped = {c[3].removeprefix("id:"): c[4] for c in modify_calls}
    assert stamped["1"] == "automatch_state=matched"
    assert stamped["2"] == "automatch_state=nomatch"
    assert stamped["3"] == "automatch_state=matched"
    # id 4 was excluded by the cap -- never touched at all.
    assert "4" not in stamped


@pytest.mark.anyio
async def test_sweep_user_with_no_candidates_does_not_import_or_rescan():
    subsonic_client = mock.AsyncMock()
    beets_exec = BeetsExec()
    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker:
        mock_docker.execute.return_value = ""
        service, *_ = _make_service(beets_exec, subsonic_client=subsonic_client)
        result = await service.sweep_user(USER)

    assert result.skipped is False
    assert result.candidates == 0
    assert result.chunks_processed == 0
    subsonic_client.scan.assert_not_awaited()


@pytest.mark.anyio
async def test_sweep_user_stamps_error_and_bumps_attempts_when_reimport_raises():
    rekordbox_xml_controller = mock.Mock()
    beets_exec = BeetsExec()

    def fake_execute(container_name, command, stream=False):
        cmd = command if isinstance(command, list) else command.split()
        if cmd[:4] == ["beet", "list", "-f", "$id"] and cmd[4:] == ["automatch_state:pending"]:
            return "1\n"
        if cmd[:4] == ["beet", "list", "-f", "$id:$automatch_attempts"]:
            return ""
        if cmd[:2] == ["beet", "-c"]:
            raise RuntimeError("musicbrainz.org unreachable")
        return ""

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker:
        mock_docker.execute.side_effect = fake_execute
        service, *_, rekordbox_xml_controller = _make_service(
            beets_exec, rekordbox_xml_controller=rekordbox_xml_controller
        )
        result = await service.sweep_user(USER)

    assert result.chunks_processed == 1  # the chunk was attempted, even though it failed
    rekordbox_xml_controller.remap_subbox_id_for_ids.assert_not_called()
    rekordbox_xml_controller.retag_duplicates.assert_not_called()

    modify_calls = [
        call.args[1] for call in mock_docker.execute.call_args_list
        if isinstance(call.args[1], list) and call.args[1][:2] == ["beet", "modify"]
    ]
    assert len(modify_calls) == 1
    assert modify_calls[0][3] == "id:1"
    assert "automatch_state=error" in modify_calls[0]
    assert "automatch_attempts=1" in modify_calls[0]


# --- chunking, yielding, wall-clock cap -----------------------------------------

@pytest.mark.anyio
async def test_sweep_user_chunks_by_chunk_size():
    beets_exec = BeetsExec()
    processed_chunks = []

    def fake_execute(container_name, command, stream=False):
        cmd = command if isinstance(command, list) else command.split()
        if cmd[:4] == ["beet", "list", "-f", "$id"] and cmd[4:] == ["automatch_state:pending"]:
            return "1\n2\n3\n"
        if cmd[:4] == ["beet", "list", "-f", "$id:$automatch_attempts"]:
            return ""
        if cmd[:2] == ["beet", "-c"]:
            processed_chunks.append([t for t in cmd if t.startswith("id:")])
            return "ok"
        if cmd[:4] == ["beet", "list", "-f", "$id:$mb_trackid"]:
            return ""
        return ""

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker:
        mock_docker.execute.side_effect = fake_execute
        service, *_ = _make_service(beets_exec, chunk_size=2)
        result = await service.sweep_user(USER)

    assert result.candidates == 3
    assert result.chunks_total == 2  # [1, 2], [3]
    assert result.chunks_processed == 2
    assert processed_chunks == [["id:1", "id:2"], ["id:3"]]


@pytest.mark.anyio
async def test_sweep_user_yields_when_foreground_work_appears_between_chunks():
    beets_exec = BeetsExec()

    def fake_execute(container_name, command, stream=False):
        cmd = command if isinstance(command, list) else command.split()
        if cmd[:4] == ["beet", "list", "-f", "$id"] and cmd[4:] == ["automatch_state:pending"]:
            return "1\n2\n"
        if cmd[:4] == ["beet", "list", "-f", "$id:$automatch_attempts"]:
            return ""
        if cmd[:2] == ["beet", "-c"]:
            return "ok"
        if cmd[:4] == ["beet", "list", "-f", "$id:$mb_trackid"]:
            return ""
        return ""

    db_controller = mock.Mock()
    # idle at the top-level check, then a foreground import appears before the
    # second chunk's yield check.
    db_controller.get_number_of_jobs.side_effect = [0, 0, 1]
    subsonic_client = mock.AsyncMock()
    subsonic_client.get_now_playing.return_value = []

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker:
        mock_docker.execute.side_effect = fake_execute
        service, *_ = _make_service(
            beets_exec, db_controller=db_controller, subsonic_client=subsonic_client, chunk_size=1
        )
        result = await service.sweep_user(USER)

    assert result.candidates == 2
    assert result.chunks_total == 2
    assert result.chunks_processed == 1  # abandoned before the second chunk
    subsonic_client.scan.assert_awaited_once_with(USER)  # still rescans for the chunk that did run


@pytest.mark.anyio
async def test_sweep_user_respects_wall_clock_budget():
    beets_exec = BeetsExec()

    def fake_execute(container_name, command, stream=False):
        cmd = command if isinstance(command, list) else command.split()
        if cmd[:4] == ["beet", "list", "-f", "$id"] and cmd[4:] == ["automatch_state:pending"]:
            return "1\n2\n"
        if cmd[:4] == ["beet", "list", "-f", "$id:$automatch_attempts"]:
            return ""
        return ""

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker:
        mock_docker.execute.side_effect = fake_execute
        service, *_ = _make_service(beets_exec, chunk_size=1, wall_clock_budget_s=0)
        result = await service.sweep_user(USER)

    assert result.candidates == 2
    assert result.chunks_processed == 0  # budget already exhausted before the first chunk
