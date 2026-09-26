import logging
import time
from dataclasses import dataclass, field
from typing import List

import anyio

from pymix.controllers.db_controller import DbController
from pymix.routers.auth import DEMO_USERNAME
from pymix.services import metrics
from pymix.services.trash import TrashService

logger = logging.getLogger(__name__)

# A delete holds its items `pending` for the seconds between its snapshot and its
# verify. One still pending an hour later belongs to a pymix that died mid-delete.
PENDING_GRACE_S = 60 * 60


@dataclass
class ReaperPass:
    n_batches_purged: int = 0
    n_missing_swept: int = 0
    errors: List[str] = field(default_factory=list)


async def run_reaper_pass(
        trash_service: TrashService, db_controller: DbController, sweep_after_s: float
) -> ReaperPass:
    """
    One pass: settle deletes that never finished, purge every expired batch, then
    sweep each user's Navidrome for missing rows no batch holds (#200, design §12).
    Every failure is collected rather than raised, so one user's broken container
    cannot stop the others being reaped, and the pass is always recorded.
    """
    started_at = time.time()
    result = ReaperPass()
    try:
        for batch_id in db_controller.stale_pending_trash_batch_ids(started_at - PENDING_GRACE_S):
            try:
                await anyio.to_thread.run_sync(trash_service.settle_pending, batch_id)
            except Exception as ex:
                logger.exception(f'trash reaper: could not settle pending batch {batch_id}')
                result.errors.append(f'batch {batch_id}: settling an interrupted delete failed: {ex!r}')

        for batch_id in db_controller.expired_trash_batch_ids(started_at):
            try:
                outcome = await trash_service.purge_batch(batch_id)
            except Exception as ex:
                logger.exception(f'trash reaper: could not purge batch {batch_id}')
                result.errors.append(f'batch {batch_id}: purge failed: {ex!r}')
                continue
            result.errors.extend(outcome.errors)
            if outcome.n_purged:
                result.n_batches_purged += 1

        for username in db_controller.get_usernames():
            # demo browses inside demoadmin's Navidrome and has none of its own.
            if username == DEMO_USERNAME:
                continue
            try:
                result.n_missing_swept += await trash_service.sweep_missing(username, sweep_after_s)
            except Exception as ex:
                logger.exception(f'trash reaper: sweep failed for {username}')
                result.errors.append(f'{username}: sweep of missing navidrome rows failed: {ex!r}')
    except Exception as ex:
        logger.exception('trash reaper: pass failed')
        result.errors.append(f'pass failed: {ex!r}')
    finally:
        try:
            db_controller.record_trash_reaper_run(
                started_at, result.n_batches_purged, result.n_missing_swept, result.errors
            )
        except Exception:
            logger.exception('trash reaper: could not record the pass')
        metrics.trash_reaper_completed(result.n_missing_swept, len(result.errors))
    logger.info(
        f'trash reaper: purged {result.n_batches_purged} batch(es), swept {result.n_missing_swept} '
        f'missing row(s), {len(result.errors)} failure(s)'
    )
    return result


async def trash_reaper_loop(
        trash_service: TrashService,
        db_controller: DbController,
        interval_s: float,
        sweep_after_s: float,
):
    """Purge the trash as it expires. Hourly by default; see run_reaper_pass."""
    while True:
        await run_reaper_pass(trash_service, db_controller, sweep_after_s)
        await anyio.sleep(interval_s)
