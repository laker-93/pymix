import logging

import anyio

from pymix.controllers.db_controller import DbController
from pymix.services.sheet_sync_service import SheetSyncService

logger = logging.getLogger(__name__)


async def sheet_sync_loop(sheet_sync_service: SheetSyncService, db_controller: DbController, poll_interval_s: int):
    while True:
        for user in db_controller.get_users_with_wishlist_sheet():
            try:
                sheet_sync_service.sync_user(user)
            except Exception:
                logger.exception(f"sheet sync: unexpected error syncing user {user['username']}")
        await anyio.sleep(poll_interval_s)
