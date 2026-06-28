import logging

import anyio

from pymix.controllers.db_controller import DbController
from pymix.services.sheet_sync_service import SheetSyncResult, SheetSyncService

logger = logging.getLogger(__name__)


def _log_cycle_summary(results: list[SheetSyncResult], n_users: int) -> None:
    """Emit a single line per poll cycle (≈ every poll_interval_s) summarising which
    users' sheets are syncing ok, instead of several lines per user per cycle."""
    if n_users == 0:
        logger.debug("sheet sync: no users with a wishlist sheet configured")
        return

    ok = [r.username for r in results if r.status == "ok"]
    errored = [r for r in results if r.status == "error"]
    total_imported = sum(r.imported for r in results)
    total_row_errors = sum(r.errors for r in results)

    parts = [f"{len(ok)}/{n_users} user(s) syncing ok"]
    if ok:
        parts[0] += ": " + ", ".join(ok)
    if total_imported:
        parts.append(f"{total_imported} item(s) imported this cycle")
    if total_row_errors:
        parts.append(f"{total_row_errors} row error(s)")
    summary = "sheet sync: " + "; ".join(parts)

    if errored:
        err_detail = "; ".join(f"{r.username} ({r.error_message})" for r in errored)
        logger.warning(f"{summary}; sheet errors: {err_detail}")
    else:
        logger.info(summary)


async def sheet_sync_loop(sheet_sync_service: SheetSyncService, db_controller: DbController, poll_interval_s: int):
    while True:
        users = db_controller.get_users_with_wishlist_sheet()
        logger.debug(f"sheet sync: starting poll cycle, {len(users)} user(s) with a wishlist sheet configured")
        results: list[SheetSyncResult] = []
        for user in users:
            try:
                results.append(await sheet_sync_service.sync_user(user))
            except Exception:
                logger.exception(f"sheet sync: unexpected error syncing user {user['username']}")
                results.append(
                    SheetSyncResult(username=user["username"], status="error", error_message="unexpected error")
                )
        _log_cycle_summary(results, len(users))
        await anyio.sleep(poll_interval_s)
