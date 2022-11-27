import asyncio
import inspect
import logging
import sys

from dependency_injector.wiring import inject, Provide
from uvicorn import Config, Server

from pymix.containers import Container
from pymix.registration import register_app
from pymix.utils.scheduled_tasks import ScheduledTasks

logger = logging.getLogger(__name__)

@inject
async def get_all_scheduled_coros(
    all_scheduled_task: ScheduledTasks = Provide[Container.scheduled_tasks]
):
    return all_scheduled_task.get_all_coros_()



async def main(app, app_config, loop):
    config = Config(app=app, loop=loop,
                    host=app_config["application_settings"]["app_host"],
                    port=app_config["application_settings"]["app_port"],
                    log_level=app_config["application_settings"]["logging_level"].lower(),
                    )
    server = Server(config)
    coros = await get_all_scheduled_coros()
    for coro in coros:
        # note the coroutine will start running at this point as the background task decorator invokes
        # create_task at the point of being called.
        # There is no need to await the coros since the server runs forever which will keep the app alive and the coros
        # dont return anything anyways.
        coro()
    server_task = loop.create_task(server.serve())
    await asyncio.gather(server_task)


if __name__ == '__main__':
    app, app_config = register_app()
    app.container.wire(modules=[__name__])
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main(app, app_config, loop))
    except Exception as ex:
        logger.warning(f"loop unexpectedly closed with error {repr(ex)}")
        loop.close()
