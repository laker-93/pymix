import logging

import anyio

from pymix.controllers.db_controller import DbController
from pymix.services.sheet_sync_service import SheetSyncService

logger = logging.getLogger(__name__)


async def sheet_sync_loop(sheet_sync_service: SheetSyncService, db_controller: DbController, poll_interval_s: int):
    while True:
        users = db_controller.get_users_with_wishlist_sheet()
        logger.info(f"sheet sync: starting poll cycle, {len(users)} user(s) with a wishlist sheet configured")
        for user in users:
            try:
                await sheet_sync_service.sync_user(user)
            except Exception:
                logger.exception(f"sheet sync: unexpected error syncing user {user['username']}")
        logger.info(f"sheet sync: poll cycle complete, sleeping {poll_interval_s}s")
        await anyio.sleep(poll_interval_s)
