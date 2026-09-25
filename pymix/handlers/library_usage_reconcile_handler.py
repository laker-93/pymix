import logging

import anyio

from pymix.clients.beets_exec import BeetsExec
from pymix.controllers.db_controller import DbController

logger = logging.getLogger(__name__)


def _reconcile_user(db_controller: DbController, beets_exec: BeetsExec, username: str) -> None:
    # Under the user's beets write lock: every import and delete that moves the
    # counter holds it too, so the walk can neither miss a file mid-move nor have
    # its absolute figure overwritten a moment later by a delta it already counted.
    with beets_exec.write_lock(f"beets{username}"):
        db_controller.reconcile_library_bytes(username)


async def library_usage_reconcile_loop(
    db_controller: DbController,
    beets_exec: BeetsExec,
    interval_s: int,
):
    """Re-walk every user's library and correct the quota counter (#183).

    The counter is moved by imports and deletes, but not by what changes a file's
    size in place -- beets embedding art, a BPM or cue write-back, a retag. This loop
    is what bounds that drift, and its first pass is the backfill for users created
    before the counter existed. It is the only O(library) work left in the quota,
    and it runs off the request path, one user at a time, on a worker thread.
    """
    while True:
        usernames = db_controller.get_usernames()
        n_failed = 0
        for username in usernames:
            try:
                await anyio.to_thread.run_sync(_reconcile_user, db_controller, beets_exec, username)
            except Exception:
                n_failed += 1
                logger.exception(f'library usage reconcile: failed for user {username}')
        logger.info(f'library usage reconcile: walked {len(usernames) - n_failed}/{len(usernames)} user(s)')
        await anyio.sleep(interval_s)
