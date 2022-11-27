import logging

from pymix.controllers.db_store_controller import DbStoreController

logger = logging.getLogger(__name__)


class ScheduledJobs:

    def __init__(self, db_controller: DbStoreController):
        self._db_controller = db_controller

    async def delete_intraday_prices(self, context) -> str:
        n_days = context['n_days']
        #n_entries_deleted = await self._db_controller.delete_intraday_prices_older_than_n_days(n_days)
        n_entries_deleted = 0
        detail = f"successfully deleted {n_entries_deleted}"
        return detail
