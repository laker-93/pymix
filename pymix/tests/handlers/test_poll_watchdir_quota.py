"""
The watch-dir poller's quota check (#183).

It used to walk the whole library on every filesystem event, and once a user went
over quota it added them to a set that nothing but a pymix restart emptied: a user
who freed space was still never imported from watch/ again.
"""
import os
import time
from unittest import mock

import anyio
import pytest
from watchfiles import Change

import pymix.handlers.filebrowser_file_handler as fbh

DEBOUNCE_SECONDS = 15


class _Clock:
    """Stands in for the module's `time`: monotonic() is driven by the test, time()
    stays real so _all_files_stable still compares against real mtimes."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    @staticmethod
    def time():
        return time.time()


def _settled_file(path, n_bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'x' * n_bytes)
    an_hour_ago = time.time() - 3600
    os.utime(path, (an_hour_ago, an_hour_ago))
    return path


async def _run_poller(tmp_path, monkeypatch, db_controller, script):
    """Drive poll_watchdir through ``script``: a list of (seconds_later, changes)
    batches, as awatch would yield them. Returns the users sent for import."""
    clock = _Clock()
    monkeypatch.setattr(fbh, 'time', clock)

    async def fake_awatch(*args, **kwargs):
        for seconds_later, changes in script:
            clock.now += seconds_later
            yield changes

    monkeypatch.setattr(fbh, 'awatch', fake_awatch)
    send, receive = anyio.create_memory_object_stream[str](100)
    await fbh.poll_watchdir(tmp_path, 'watch', send, db_controller)
    async with receive:
        return [user async for user in receive]


@pytest.mark.anyio
async def test_an_over_quota_user_is_imported_again_once_they_have_room(tmp_path, monkeypatch):
    first = _settled_file(tmp_path / 'dj' / 'watch' / 'a.mp3', 100)
    db_controller = mock.Mock()
    # Over quota on the first pass; the user then deletes tracks and has room.
    db_controller.user_library_size_exceeded.side_effect = [(True, 1000, 950), (False, 1000, 100)]

    second = _settled_file(tmp_path / 'dj' / 'watch' / 'b.mp3', 50)
    sent = await _run_poller(tmp_path, monkeypatch, db_controller, [
        (0, {(Change.added, str(first))}),
        (DEBOUNCE_SECONDS, set()),             # debounce passes: over quota, dropped
        (60, {(Change.added, str(second))}),   # a new upload after freeing space
        (DEBOUNCE_SECONDS, set()),             # judged afresh, not latched
    ])

    assert sent == ['dj']
    assert db_controller.user_library_size_exceeded.call_count == 2
    # The second pass is charged for everything waiting in watch/, including the
    # file the dropped pass left there -- that is what the import would stage.
    assert db_controller.user_library_size_exceeded.call_args_list[1].args == ('dj', 150)


@pytest.mark.anyio
async def test_the_quota_is_checked_once_per_pass_not_per_event(tmp_path, monkeypatch):
    files = [_settled_file(tmp_path / 'dj' / 'watch' / f'{i}.mp3', 10) for i in range(5)]
    db_controller = mock.Mock()
    db_controller.user_library_size_exceeded.return_value = (False, 1000, 0)

    sent = await _run_poller(tmp_path, monkeypatch, db_controller, [
        (0, {(Change.added, str(f)) for f in files}),
        (1, {(Change.modified, str(f)) for f in files}),
        (DEBOUNCE_SECONDS, set()),
    ])

    assert sent == ['dj']
    db_controller.user_library_size_exceeded.assert_called_once_with('dj', 50)


@pytest.mark.anyio
async def test_a_failing_check_skips_the_pass_and_keeps_the_poller_alive(tmp_path, monkeypatch):
    # e.g. a directory under user_root with no user row behind it
    stray = _settled_file(tmp_path / 'not-a-user' / 'watch' / 'a.mp3', 10)
    real = _settled_file(tmp_path / 'dj' / 'watch' / 'a.mp3', 10)
    db_controller = mock.Mock()

    def check(user, n_bytes):
        if user == 'not-a-user':
            raise AssertionError('found 0 users with username not-a-user')
        return False, 1000, 0

    db_controller.user_library_size_exceeded.side_effect = check

    sent = await _run_poller(tmp_path, monkeypatch, db_controller, [
        (0, {(Change.added, str(stray)), (Change.added, str(real))}),
        (DEBOUNCE_SECONDS, set()),
    ])

    assert sent == ['dj']
