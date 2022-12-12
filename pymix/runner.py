import asyncio
import logging

from uvicorn import Config, Server

from pymix.registration import register_app

logger = logging.getLogger(__name__)


async def main(app, app_config, loop):
    config = Config(app=app, loop=loop,
                    host=app_config["application_settings"]["app_host"],
                    port=app_config["application_settings"]["app_port"],
                    log_level=app_config["application_settings"]["logging_level"].lower(),
                    )
    server = Server(config)
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
