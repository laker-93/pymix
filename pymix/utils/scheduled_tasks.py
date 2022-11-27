import datetime
import inspect

from toredocore.providers.job.job_runner import run_job

from toredocore.utilities.decorators import background_task
from toredocore.utilities.runner import run_async

from pymix.db_model.job_database_gateway import JobDatabaseGateway
from pymix.utils.scheduled_jobs import ScheduledJobs


class ScheduledTasks:
    def __init__(self, scheduled_jobs: ScheduledJobs, job_db_gw: JobDatabaseGateway):
        self._scheduled_jobs = scheduled_jobs
        self._job_db_gw = job_db_gw

    def get_all_coros_(
            self
    ):
        scheduled_coros = []
        for fn in dir(self):
            # this adds all coroutines in this class (apart from this fn) that don't start or end with underscores to
            # the list of coroutines that will be scheduled to begin on application startup.
            if not fn.startswith('__') and fn != 'get_all_coros' and not fn.startswith('_') and not fn.endswith('_'):
                coro = self.__getattribute__(fn)
                if inspect.ismethod(coro):
                    # note the coroutine will not start running until it is called by the caller of this method
                    scheduled_coros.append(coro)
        return scheduled_coros

    async def _run_job(self, coro, context, create_job_fn_name: str = None, update_job_fn_name: str = None):
        """
        This implements the logic of using the run_job decorator from toredocore with the Apollo application's job db gw
        to store the job results. Note it can't be used as a decorator since we need to pass in the job_db which is
        only available after this class is instantiated.
        :return:
        """
        job_db = self._job_db_gw
        run_job_decorator = run_job(job_db, create_job_fn_name, update_job_fn_name)
        job_coro = run_job_decorator(coro)
        result = await job_coro(context=context)
        return result

    # “At 22:00 on every day-of-week from Monday through Friday.”
    @background_task(cron="0 22 * * 1-5", start_time="22:00")
    async def delete_intraday_prices(
            self
    ):

        coro = self._scheduled_jobs.delete_intraday_prices
        # processing yesterday's trades that were uploaded to IB FTP today
        context = {'n_days': 1}
        result = await self._run_job(coro, context)
        return result

    # This only needs to run once at startup.
    @background_task(cron="* * * * *", max_repetitions=1)
    async def start_dispatcher(self):
        context = {}
        await self._scheduled_jobs.start_dispatcher(context)