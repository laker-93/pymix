"""The trash reaper (#200): purges what has expired, and reports itself, since
nothing calls it over HTTP."""
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from pymix.handlers.trash_reaper_handler import run_reaper_pass
from pymix.services import metrics
from pymix.services.trash import PurgeOutcome


def _db(expired=(), usernames=('dj', 'demo', 'broken')):
    db = MagicMock()
    db.stale_pending_trash_batch_ids.return_value = []
    db.expired_trash_batch_ids.return_value = list(expired)
    db.get_usernames.return_value = list(usernames)
    return db


def _failures():
    return metrics.REGISTRY.get_sample_value('pymix_trash_reaper_failures_total') or 0.0


@pytest.mark.anyio
async def test_a_pass_purges_every_expired_batch_and_sweeps_every_user_but_demo():
    db = _db(expired=['b1', 'b2'], usernames=('dj', 'demo'))
    trash = MagicMock()
    trash.purge_batch = AsyncMock(side_effect=lambda batch_id: PurgeOutcome(batch_id=batch_id, n_purged=1))
    trash.sweep_missing = AsyncMock(return_value=3)

    result = await run_reaper_pass(trash, db, sweep_after_s=86400)

    assert [c.args[0] for c in trash.purge_batch.await_args_list] == ['b1', 'b2']
    assert db.expired_trash_batch_ids.call_args.args[0] == pytest.approx(time.time(), abs=5)
    # demo browses demoadmin's Navidrome; it has none of its own to sweep.
    assert [c.args[0] for c in trash.sweep_missing.await_args_list] == ['dj']
    assert (result.n_batches_purged, result.n_missing_swept, result.errors) == (2, 3, [])
    db.record_trash_reaper_run.assert_called_once()
    assert db.record_trash_reaper_run.call_args.args[1:] == (2, 3, [])


@pytest.mark.anyio
async def test_one_broken_user_neither_stops_the_pass_nor_goes_unrecorded():
    db = _db(expired=['bad', 'good'], usernames=('broken', 'dj'))
    trash = MagicMock()

    async def purge(batch_id):
        if batch_id == 'bad':
            raise RuntimeError('disk on fire')
        return PurgeOutcome(batch_id=batch_id, n_purged=1)
    trash.purge_batch = AsyncMock(side_effect=purge)

    async def sweep(username, older_than_s):
        if username == 'broken':
            raise RuntimeError('container down')
        return 1
    trash.sweep_missing = AsyncMock(side_effect=sweep)
    before = _failures()

    result = await run_reaper_pass(trash, db, sweep_after_s=86400)

    assert result.n_batches_purged == 1 and result.n_missing_swept == 1
    assert len(result.errors) == 2
    assert 'bad' in result.errors[0] and 'broken' in result.errors[1]
    # A failed pass writes a row naming what failed, not just a log line.
    assert db.record_trash_reaper_run.call_args.args[3] == result.errors
    assert _failures() - before == 2


@pytest.mark.anyio
async def test_interrupted_deletes_are_settled_first():
    db = _db()
    db.stale_pending_trash_batch_ids.return_value = ['p1']
    trash = MagicMock()
    trash.purge_batch = AsyncMock()
    trash.sweep_missing = AsyncMock(return_value=0)

    await run_reaper_pass(trash, db, sweep_after_s=86400)

    trash.settle_pending.assert_called_once_with('p1')
