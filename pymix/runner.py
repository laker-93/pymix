import asyncio
import logging
from argparse import ArgumentParser
from typing import Dict

from dependency_injector.wiring import inject, Provide
from uvicorn import Config, Server

from pymix.containers import Container
from pymix.registration import create_app, create_container

logger = logging.getLogger(__name__)


@inject
async def main(loop, app_config: Dict = Provide[Container.config]):
    app = create_app()
    config = Config(app=app, loop=loop,
                    host=app_config["application_settings"]["app_host"],
                    port=app_config["application_settings"]["app_port"],
                    log_level=app_config["application_settings"]["logging_level"].lower()
                    )
    server = Server(config)
    server_task = loop.create_task(server.serve())
    await asyncio.gather(server_task)


if __name__ == '__main__':
    arg_parser = ArgumentParser()
    arg_parser.add_argument("-e", "--env", default="dev.old", help="The environment to run the app with")
    args = arg_parser.parse_args()
    container = create_container(args.env)
    container.wire(modules=[__name__])
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main(loop))
    except Exception as ex:
        logger.warning(f"loop unexpectedly closed with error {repr(ex)}")
        loop.close()
