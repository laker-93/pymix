"""
Tests for the on-demand manual reimport (laker-93/pymix#95): resolving a caller's
beets query, running the reimport under the write lock, re-mapping/re-tagging, and
reporting matched/nomatch state back -- with no idle check, chunking, or budget,
since the caller is waiting synchronously on a single bounded request.

Uses a real BeetsExec (so write_lock genuinely serializes, same pattern as
test_beets_exec.py / test_services_orchestrator_beets_migration.py) with the
underlying docker.execute call patched, plus mocked subsonic_client /
rekordbox_xml_controller collaborators.
"""
from unittest import mock

import pytest

from pymix.clients.beets_exec import BeetsExec
from pymix.services.automatch_service import AutomatchService

USER = {"username": "demoadmin", "password": "pw"}


def _make_service(beets_exec, subsonic_client=None, rekordbox_xml_controller=None):
    if subsonic_client is None:
        subsonic_client = mock.AsyncMock()
    rekordbox_xml_controller = rekordbox_xml_controller or mock.Mock()
    service = AutomatchService(
        beets_exec=beets_exec,
        subsonic_client=subsonic_client,
        rekordbox_xml_controller=rekordbox_xml_controller,
    )
    return service, subsonic_client, rekordbox_xml_controller


@pytest.mark.anyio
async def test_manual_reimport_with_no_matching_tracks_does_not_import():
    subsonic_client = mock.AsyncMock()
    beets_exec = BeetsExec()

    def fake_execute(container_name, command, stream=False):
        cmd = command if isinstance(command, list) else command.split()
        if cmd[:4] == ["beet", "list", "-f", "$id"] and cmd[4:] == ["path:Nonexistent"]:
            return ""
        return ""

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker:
        mock_docker.execute.side_effect = fake_execute
        service, *_ = _make_service(beets_exec, subsonic_client=subsonic_client)
        result = await service.manual_reimport(USER, "path:Nonexistent")

    assert result.matched == []
    assert result.nomatch == []
    assert result.errored is False
    subsonic_client.scan.assert_not_awaited()


@pytest.mark.anyio
async def test_manual_reimport_resolves_query_reimports_and_reports_state():
    rekordbox_xml_controller = mock.Mock()
    subsonic_client = mock.AsyncMock()
    beets_exec = BeetsExec()

    def fake_execute(container_name, command, stream=False):
        cmd = command if isinstance(command, list) else command.split()
        if cmd[:4] == ["beet", "list", "-f", "$id"] and cmd[4:] == ["path:Artist/Album"]:
            return "1\n2\n"
        if cmd[:2] == ["beet", "-c"]:
            return "import ok"
        if cmd[:4] == ["beet", "list", "-f", "$id:$mb_trackid"]:
            return "1:mbid-1\n2:\n"
        return ""

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker:
        mock_docker.execute.side_effect = fake_execute
        service, subsonic_client, rekordbox_xml_controller = _make_service(
            beets_exec, subsonic_client=subsonic_client, rekordbox_xml_controller=rekordbox_xml_controller
        )
        result = await service.manual_reimport(USER, "path:Artist/Album")

    assert result.matched == [1]
    assert result.nomatch == [2]
    assert result.errored is False
    rekordbox_xml_controller.remap_subbox_id_for_ids.assert_called_once_with("demoadmin", [1, 2], public=False)
    rekordbox_xml_controller.retag_duplicates.assert_called_once_with("demoadmin", public=False)
    subsonic_client.scan.assert_awaited_once_with(USER)

    modify_calls = [
        call.args[1] for call in mock_docker.execute.call_args_list
        if isinstance(call.args[1], list) and call.args[1][:2] == ["beet", "modify"]
    ]
    # -M (never move) on every restamp -- see #94.
    assert all(c[:4] == ["beet", "modify", "-y", "-M"] for c in modify_calls)
    stamped = {c[4].removeprefix("id:"): c[5] for c in modify_calls}
    assert stamped["1"] == "automatch_state=matched"
    assert stamped["2"] == "automatch_state=nomatch"


@pytest.mark.anyio
async def test_manual_reimport_reports_error_when_reimport_raises():
    rekordbox_xml_controller = mock.Mock()
    subsonic_client = mock.AsyncMock()
    beets_exec = BeetsExec()

    def fake_execute(container_name, command, stream=False):
        cmd = command if isinstance(command, list) else command.split()
        if cmd[:4] == ["beet", "list", "-f", "$id"] and cmd[4:] == ["path:Artist/Album"]:
            return "1\n"
        if cmd[:2] == ["beet", "-c"]:
            raise RuntimeError("musicbrainz.org unreachable")
        return ""

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker:
        mock_docker.execute.side_effect = fake_execute
        service, subsonic_client, rekordbox_xml_controller = _make_service(
            beets_exec, subsonic_client=subsonic_client, rekordbox_xml_controller=rekordbox_xml_controller
        )
        result = await service.manual_reimport(USER, "path:Artist/Album")

    assert result.errored is True
    assert result.matched == []
    assert result.nomatch == []
    rekordbox_xml_controller.remap_subbox_id_for_ids.assert_not_called()
    rekordbox_xml_controller.retag_duplicates.assert_not_called()
    subsonic_client.scan.assert_not_awaited()


@pytest.mark.anyio
async def test_manual_reimport_reports_error_when_query_resolution_raises():
    beets_exec = BeetsExec()

    def fake_execute(container_name, command, stream=False):
        cmd = command if isinstance(command, list) else command.split()
        if cmd[:4] == ["beet", "list", "-f", "$id"]:
            raise RuntimeError("beets: malformed query")
        return ""

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker:
        mock_docker.execute.side_effect = fake_execute
        service, *_ = _make_service(beets_exec)
        result = await service.manual_reimport(USER, "not a valid query :::")

    assert result.errored is True
    assert result.matched == []
    assert result.nomatch == []


@pytest.mark.anyio
async def test_manual_reimport_splits_query_on_whitespace_like_a_beet_cli_invocation():
    """A multi-term query (e.g. two artists ORed together) should reach beets as
    separate argv elements, exactly like typing `beet list <query>` at a shell --
    not as one glued-together string beets would fail to parse as intended."""
    beets_exec = BeetsExec()
    seen_query = []

    def fake_execute(container_name, command, stream=False):
        cmd = command if isinstance(command, list) else command.split()
        if cmd[:4] == ["beet", "list", "-f", "$id"]:
            seen_query.extend(cmd[4:])
            return ""
        return ""

    with mock.patch("pymix.clients.beets_exec.docker") as mock_docker:
        mock_docker.execute.side_effect = fake_execute
        service, *_ = _make_service(beets_exec)
        await service.manual_reimport(USER, "artist:Foo , artist:Bar")

    assert seen_query == ["artist:Foo", ",", "artist:Bar"]
