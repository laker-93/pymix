"""Tests for the periodic memory sampler.

The loop itself is a `while True` around a sleep, so what's worth testing is the two
decisions it makes each pass: what the line says, and whether it is a warning. The
warning is the whole point — it is the last thing written before an OOM SIGKILL, so it
has to fire on the right side of the threshold and has to survive missing inputs (no
cgroup limit set, no glibc allocator stats) rather than raising inside the loop.
"""
import logging
from unittest import mock

import pytest

from pymix.handlers import mem_watch_handler
from pymix.handlers.mem_watch_handler import _format_sample, mem_watch_loop

_PROC = {
    "rss_mb": 275.8,
    "rss_peak_mb": 402.1,
    "rss_anon_mb": 249.4,
    "rss_file_mb": 17.7,
    "vm_data_mb": 500.0,
    "threads": 9,
}
_ALLOC = {"available": True, "in_use_mb": 83.6, "retained_mb": 56.2}


def _cgroup(current_mb, limit_mb):
    return {
        "available": True,
        "cgroup_version": 2,
        "current_mb": current_mb,
        "limit_mb": limit_mb,
        "used_fraction": round(current_mb / limit_mb, 4),
    }


def test_format_sample_reports_peak_alongside_current():
    line = _format_sample(_PROC, _ALLOC, _cgroup(300.0, 512.0))

    # The peak is what a mem_limit has to clear, so it belongs on the same line as the
    # steady state rather than only in /admin/memory (laker-93/pymix#125).
    assert "rss 275.8 MB" in line
    assert "peak 402.1 MB" in line
    assert "cgroup 300.0/512.0 MB (59%)" in line


def test_format_sample_says_unlimited_when_no_limit_is_set():
    unlimited = {
        "available": True,
        "cgroup_version": 2,
        "current_mb": 300.0,
        "limit_mb": None,
        "used_fraction": None,
    }

    assert "cgroup unlimited" in _format_sample(_PROC, _ALLOC, unlimited)


def test_format_sample_omits_allocator_numbers_on_musl():
    line = _format_sample(_PROC, {"available": False, "reason": "no mallinfo2"}, _cgroup(300.0, 512.0))

    assert "in_use" not in line
    assert "rss 275.8 MB" in line


async def _run_one_pass(monkeypatch, cgroup, warn_fraction=0.8):
    """Drive exactly one iteration: anyio.sleep is patched to abort the infinite loop."""
    monkeypatch.setattr(mem_watch_handler.memdiag, "process_memory", lambda: _PROC)
    monkeypatch.setattr(mem_watch_handler.memdiag, "allocator_stats", lambda: _ALLOC)
    monkeypatch.setattr(mem_watch_handler.memdiag, "cgroup_memory", lambda: cgroup)

    async def _stop(_):
        raise _Stop()

    monkeypatch.setattr(mem_watch_handler.anyio, "sleep", _stop)
    with pytest.raises(_Stop):
        await mem_watch_loop(60, warn_fraction)


class _Stop(Exception):
    pass


@pytest.mark.anyio
async def test_sample_stays_at_info_below_the_threshold(monkeypatch, caplog):
    with caplog.at_level(logging.INFO, logger=mem_watch_handler.__name__):
        await _run_one_pass(monkeypatch, _cgroup(300.0, 512.0))

    assert [r.levelno for r in caplog.records] == [logging.INFO]


@pytest.mark.anyio
async def test_sample_warns_once_over_the_threshold(monkeypatch, caplog):
    with caplog.at_level(logging.INFO, logger=mem_watch_handler.__name__):
        await _run_one_pass(monkeypatch, _cgroup(440.0, 512.0))

    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    # It has to name the follow-up, because by the time anyone reads it the process
    # that could have answered is gone.
    assert "/admin/memory" in caplog.records[0].message


@pytest.mark.anyio
async def test_unlimited_cgroup_cannot_warn(monkeypatch, caplog):
    """Today's prod state: no mem_limit, so there is nothing to be a fraction of. The
    loop must still produce its history rather than warning on every pass or none."""
    unlimited = {
        "available": True,
        "cgroup_version": 2,
        "current_mb": 1900.0,
        "limit_mb": None,
        "used_fraction": None,
    }
    with caplog.at_level(logging.INFO, logger=mem_watch_handler.__name__):
        await _run_one_pass(monkeypatch, unlimited)

    assert [r.levelno for r in caplog.records] == [logging.INFO]


@pytest.mark.anyio
async def test_sampling_failure_does_not_escape_the_loop(monkeypatch, caplog):
    """A diagnostic must never be the reason the app dies — the failure is logged and
    the next pass is still attempted."""
    def _boom():
        raise OSError("procfs went away")

    monkeypatch.setattr(mem_watch_handler.memdiag, "process_memory", _boom)

    async def _stop(_):
        raise _Stop()

    monkeypatch.setattr(mem_watch_handler.anyio, "sleep", _stop)
    with caplog.at_level(logging.INFO, logger=mem_watch_handler.__name__):
        with pytest.raises(_Stop):
            await mem_watch_loop(60, 0.8)

    assert caplog.records[0].levelno == logging.ERROR
    assert "sampling failed" in caplog.records[0].message
