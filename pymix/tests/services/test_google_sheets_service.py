"""Regression tests for laker-93/pymix#81.

The Sheets client is synchronous (googleapiclient -> httplib2). Calling it from
the event loop wedged the entire service for ~9 minutes in prod when a keep-alive
socket went half-open, because httplib2 was given no timeout. These tests pin the
three properties that prevent a repeat: the call runs off the loop, a transport
error drops the poisoned connection, and the transport is built with a timeout.
"""
import threading
import time

import httplib2
import pytest

import pymix.services.google_sheets_service as gss
from pymix.services.google_sheets_service import GoogleSheetsService


class _FakeValues:
    def __init__(self, on_execute, result):
        self._on_execute = on_execute
        self._result = result

    def _call(self, **kwargs):
        on_execute, result = self._on_execute, self._result

        class _Request:
            def execute(self):
                on_execute()
                return result

        return _Request()

    get = _call
    update = _call


class _FakeSpreadsheets:
    def __init__(self, values):
        self._values = values

    def values(self):
        return self._values


class _FakeService:
    def __init__(self, on_execute=lambda: None, result=None):
        self._spreadsheets = _FakeSpreadsheets(_FakeValues(on_execute, result or {}))
        # Counted because constructing the real resource is what costs ~18.7 MB.
        self.spreadsheets_calls = 0

    def spreadsheets(self):
        self.spreadsheets_calls += 1
        return self._spreadsheets


def _patch_build(monkeypatch, service_factory):
    """Let the real _get_service() run, but with credentials/build stubbed out."""
    monkeypatch.setattr(
        gss.service_account.Credentials,
        "from_service_account_file",
        staticmethod(lambda *a, **k: object()),
    )
    monkeypatch.setattr(gss, "google_auth_httplib2", _StubAuthedHttpModule())
    monkeypatch.setattr(gss, "build", lambda *a, **k: service_factory())


class _StubAuthedHttpModule:
    @staticmethod
    def AuthorizedHttp(credentials, http):
        return http


@pytest.mark.anyio
async def test_read_rows_does_not_block_the_event_loop(monkeypatch):
    """The blocking Sheets call must run in a worker thread.

    Pre-fix this deadlocked: read_rows() held the loop, so the ticker could never
    run to release the gate, and the test only unwedged on the 5s wait timeout.
    """
    gate = threading.Event()
    _patch_build(monkeypatch, lambda: _FakeService(on_execute=lambda: gate.wait(5)))
    service = GoogleSheetsService("/nonexistent.json")

    ticks = 0

    async def ticker():
        nonlocal ticks
        import anyio
        for _ in range(5):
            ticks += 1
            await anyio.sleep(0.01)
        gate.set()

    import anyio
    started = time.monotonic()
    async with anyio.create_task_group() as tg:
        tg.start_soon(ticker)
        await service.read_rows("sheet-id")
    elapsed = time.monotonic() - started

    assert ticks == 5, "event loop was starved while the Sheets call was in flight"
    assert elapsed < 2, f"call did not run concurrently with the loop (took {elapsed:.2f}s)"


class _FakeConn:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


@pytest.mark.anyio
async def test_transport_error_drops_the_socket_but_keeps_the_client(monkeypatch):
    """A BrokenPipeError poisons the pooled TLS socket, so the socket must go (#81).

    The client must *not* go with it (#116). Rebuilding it strands ~52 MB of
    generated docstrings per failure, which is what grew prod to ~1 GB.
    """
    builds = []

    def factory():
        builds.append(1)

        def boom():
            raise BrokenPipeError(32, "Broken pipe")

        return _FakeService(on_execute=boom)

    _patch_build(monkeypatch, factory)
    service = GoogleSheetsService("/nonexistent.json")

    with pytest.raises(BrokenPipeError):
        await service.read_rows("sheet-id")

    # A pooled connection only exists once a request has been made, so inject one
    # and drive a second failure to prove the reset actually closes it.
    conn = _FakeConn()
    service._http.connections["https:sheets.googleapis.com"] = conn
    with pytest.raises(BrokenPipeError):
        await service.read_rows("sheet-id")

    assert conn.closed, "poisoned socket was left open — #81 regression"
    assert service._http.connections == {}, "connection pool was not cleared"
    assert len(builds) == 1, "client was rebuilt after a transport error — #116 leak"


@pytest.mark.anyio
async def test_repeated_transport_errors_never_rebuild_the_client(monkeypatch):
    """The leak was proportional to failure count: 10 broken pipes, ~520 MB stranded."""
    builds = []

    def factory():
        builds.append(1)

        def boom():
            raise BrokenPipeError(32, "Broken pipe")

        return _FakeService(on_execute=boom)

    _patch_build(monkeypatch, factory)
    service = GoogleSheetsService("/nonexistent.json")

    for _ in range(10):
        with pytest.raises(BrokenPipeError):
            await service.read_rows("sheet-id")

    assert len(builds) == 1, f"built the client {len(builds)}x for 10 failures"


@pytest.mark.anyio
async def test_the_nested_resource_is_constructed_once(monkeypatch):
    """Each `spreadsheets().values()` renders ~18.7 MB of docstrings, so cache it.

    Pre-fix this ran inside every request lambda, re-paying that on every poll.
    """
    service_obj = _FakeService()
    _patch_build(monkeypatch, lambda: service_obj)
    service = GoogleSheetsService("/nonexistent.json")

    await service.read_rows("sheet-id")
    await service.read_rows("sheet-id")
    await service.write_status("sheet-id", 2, "Added", "2026-01-01")

    assert service_obj.spreadsheets_calls == 1, (
        f"constructed the resource {service_obj.spreadsheets_calls}x across 3 calls"
    )


@pytest.mark.anyio
async def test_healthy_calls_reuse_one_connection(monkeypatch):
    """The redial path must not cost a rebuild on every ordinary call."""
    builds = []
    _patch_build(monkeypatch, lambda: (builds.append(1), _FakeService())[1])
    service = GoogleSheetsService("/nonexistent.json")

    await service.read_rows("sheet-id")
    await service.read_rows("sheet-id")

    assert len(builds) == 1


@pytest.mark.anyio
async def test_transport_is_built_with_an_explicit_timeout(monkeypatch):
    """httplib2 blocks forever unless it is handed a timeout — that was the bug."""
    seen = {}
    real_http = httplib2.Http

    def recording_http(*args, **kwargs):
        seen.update(kwargs)
        return real_http(*args, **kwargs)

    monkeypatch.setattr(gss.httplib2, "Http", recording_http)
    _patch_build(monkeypatch, _FakeService)
    service = GoogleSheetsService("/nonexistent.json", timeout_s=17)

    await service.read_rows("sheet-id")

    assert seen.get("timeout") == 17


@pytest.mark.anyio
async def test_rows_are_padded_and_stripped(monkeypatch):
    """Short rows come back from Sheets truncated, not blank-padded."""
    result = {"values": [["  note ", " artist", "title ", " url ", "Added", "ts"], ["just-a-note"]]}
    _patch_build(monkeypatch, lambda: _FakeService(result=result))
    service = GoogleSheetsService("/nonexistent.json")

    rows = await service.read_rows("sheet-id")

    assert rows[0] == {
        "row_index": 2, "raw_note": "note", "artist": "artist",
        "title": "title", "url": "url", "status": "Added",
    }
    assert rows[1] == {
        "row_index": 3, "raw_note": "just-a-note", "artist": "",
        "title": "", "url": "", "status": "",
    }
