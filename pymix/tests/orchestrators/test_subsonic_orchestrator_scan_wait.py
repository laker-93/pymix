"""
An import must not carry on until Navidrome has actually indexed what was just
imported: playlist creation, the rated pass and the cue/metadata pass all find
their tracks by querying Navidrome, so anything still unindexed is simply not
found and is skipped by an import that then reports success.

This used to be a flat `sleep(2)`, wrong in both directions -- wasted on a small
import, expired long before a large one had finished indexing. scan_and_wait
follows the real scan instead.
"""
from unittest import mock

import pytest

from pymix.orchestrators.subsonic_orchestrator import SubsonicOrchestrator


def _status(scanning, count=0, last_scan="T0"):
    return {"scanning": scanning, "count": count, "lastScan": last_scan}


def _orchestrator(statuses):
    """An orchestrator whose getScanStatus returns each of `statuses` in turn
    (repeating the last one once exhausted, so a poll loop can't run off the end)."""
    client = mock.Mock()
    client.scan = mock.AsyncMock(return_value=True)
    remaining = list(statuses)

    async def fake_get_scan_status(user):
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    client.get_scan_status = mock.AsyncMock(side_effect=fake_get_scan_status)
    return SubsonicOrchestrator(client), client


USER = {"username": "u", "password": "p"}


@pytest.mark.anyio
async def test_waits_for_a_scan_that_is_still_running():
    # baseline, then scanning, then done with lastScan moved on.
    orchestrator, client = _orchestrator([
        _status(False, 0, "T0"),
        _status(True, 10, "T0"),
        _status(True, 60, "T0"),
        _status(False, 99, "T1"),
    ])

    assert await orchestrator.scan_and_wait(USER, poll_interval_s=0) is True
    client.scan.assert_awaited_once()


@pytest.mark.anyio
async def test_does_not_return_before_the_scan_it_triggered_has_started():
    # The race the old sleep hid: Navidrome reports scanning=false between accepting
    # startScan and beginning work. Returning there would wait for nothing at all.
    # lastScan is unchanged for those polls, so it must keep waiting.
    orchestrator, client = _orchestrator([
        _status(False, 0, "T0"),   # baseline
        _status(False, 0, "T0"),   # accepted but not started -- must NOT satisfy
        _status(False, 0, "T0"),
        _status(True, 40, "T0"),   # now running
        _status(False, 99, "T1"),  # finished
    ])

    assert await orchestrator.scan_and_wait(USER, poll_interval_s=0) is True
    # baseline + 4 polls: it did not stop at the two not-started-yet reads.
    assert client.get_scan_status.await_count == 5


@pytest.mark.anyio
async def test_returns_as_soon_as_last_scan_moves_on():
    # A scan fast enough that we never observe scanning=true still has to be
    # detected, or we'd wait out the whole timeout for nothing.
    orchestrator, _ = _orchestrator([
        _status(False, 0, "T0"),
        _status(False, 99, "T1"),
    ])

    assert await orchestrator.scan_and_wait(USER, poll_interval_s=0) is True


@pytest.mark.anyio
async def test_handles_a_library_that_has_never_been_scanned():
    # No lastScan at all on a brand-new user's library; the first completed scan
    # introduces the field, which is still a change from the baseline.
    orchestrator, _ = _orchestrator([
        {"scanning": False, "count": 0},
        {"scanning": True, "count": 5},
        {"scanning": False, "count": 99, "lastScan": "T1"},
    ])

    assert await orchestrator.scan_and_wait(USER, poll_interval_s=0) is True


@pytest.mark.anyio
async def test_gives_up_without_raising_when_the_scan_never_reports_finishing():
    # Degrades to what the old sleep did every time (match against a possibly
    # partial library) rather than failing the import outright.
    orchestrator, _ = _orchestrator([_status(True, 1, "T0")])

    assert await orchestrator.scan_and_wait(USER, timeout_s=0.05, poll_interval_s=0.01) is False


@pytest.mark.anyio
async def test_keeps_polling_through_a_transient_status_read_failure():
    # get_scan_status returns None on a failed read; one miss must not be read as
    # "finished" nor abandon the wait.
    orchestrator, _ = _orchestrator([
        _status(False, 0, "T0"),
        None,
        _status(True, 10, "T0"),
        None,
        _status(False, 99, "T1"),
    ])

    assert await orchestrator.scan_and_wait(USER, poll_interval_s=0) is True


@pytest.mark.anyio
async def test_still_triggers_the_scan_when_the_baseline_read_fails():
    orchestrator, client = _orchestrator([
        None,                      # baseline unreadable
        _status(True, 10, "T1"),
        _status(False, 99, "T1"),
    ])

    assert await orchestrator.scan_and_wait(USER, poll_interval_s=0) is True
    client.scan.assert_awaited_once()
