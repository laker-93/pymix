import os
from contextlib import asynccontextmanager

import anyio
import logging
from logging import handlers
import yaml
import sys
from pathlib import Path

from anyio import create_memory_object_stream
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from pymix import constants
from pymix.containers import Container
from pymix.handlers.filebrowser_file_handler import poll_watchdir, trigger_processing
from pymix.routers import maintenance, create, user, beets_import, rb_import_export, serato_import_export, export_progress, sync, match_tracks, track


logger = logging.getLogger(__name__)


def initialise_logger(app_name, level="DEBUG", **kwargs):
    """
    Initialises the logger for the application with default handlers
    :param app_name:
    :param level:
    :param kwargs:
    :return:
    """
    logger = logging.getLogger()
    logger.setLevel(level)

    # create console handler and set level to debug
    ch = logging.StreamHandler()
    ch.setLevel(level)

    # create formatter
    columns = [
        "%(asctime)s",
        app_name,
        "%(levelname)s",
        "%(process)d",
        "%(thread)d",
        "[%(filename)s-%(funcName)s():%(lineno)d]",
        "%(message)s",
    ]
    delimiter = kwargs.get("delimiter", "|")

    log_file_format = delimiter.join(columns)
    formatter = logging.Formatter(log_file_format)
    # add formatter to ch
    ch.setFormatter(formatter)
    # add ch to logger
    logger.addHandler(ch)

    if kwargs.get("disable_file_handler", False) is False:
        logs_directory = kwargs.get("logs_directory", "logs")
        when = kwargs.get("rolling_when", "midnight")
        interval = kwargs.get("rolling_interval", 1)
        backup_count = kwargs.get("rolling_backupcount", 10)

        Path(logs_directory).mkdir(exist_ok=True)

        logger.info(f"Started Writing Logs To {os.path.abspath(logs_directory)}")

        logHandler = handlers.TimedRotatingFileHandler(
            f"{logs_directory}/{app_name}.log",
            when=when,
            interval=interval,
            backupCount=backup_count,
        )
        logHandler.setLevel(logging.INFO)
        logHandler.setFormatter(formatter)
        logger.addHandler(logHandler)
    return logger

@asynccontextmanager
async def lifespan(app: FastAPI, container):
    # todo create anyio mem obj stream
    send_stream, receive_stream = create_memory_object_stream[tuple[str, list[Path]]]()

    db_controller = container.db_controller()
    rb_xml_controller = await container.rekordbox_xml_controller()
    directory = Path(container.config()['containers']['filebrowser']['user_root'])
    users = [subdir.name for subdir in directory.iterdir() if subdir.is_dir()]
    watchdir = container.config()['containers']['filebrowser']['data_watch']
    watchpaths = [Path(watchdir.format(user=user)) for user in users]
    for p in watchpaths:
        p.mkdir(parents=True, exist_ok=True)
    async with anyio.create_task_group() as tg:
        tg.start_soon(poll_watchdir, watchpaths, send_stream, db_controller)
        tg.start_soon(trigger_processing, receive_stream, rb_xml_controller)
        yield

def create_app(container):
    app = FastAPI(
        title=constants.title, version=constants.version, description=constants.description,
        lifespan=lambda app: lifespan(app, container),
        # required for dev testing
        root_path="/pymix"
    )
    app.add_middleware(
        CORSMiddleware,
        # need to set explicit origins here rather than * since feishin will be sending credentials in cookies:
        # https://stackoverflow.com/questions/18642828/origin-origin-is-not-allowed-by-access-control-allow-origin
        allow_origins=["http://localhost:4343", "https://sub-box.net", "https://www.sub-box.net", "http://localhost", "https://www.staging.sub-box.net", "https://staging.sub-box.net"],
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Set-Cookie"],
    )

    app.include_router(maintenance.router)
    app.include_router(create.router)
    app.include_router(user.router)
    app.include_router(beets_import.router)
    app.include_router(rb_import_export.router)
    app.include_router(serato_import_export.router)
    app.include_router(export_progress.router)
    app.include_router(sync.router)
    app.include_router(match_tracks.router)
    app.include_router(track.router)
    return app


def create_container(environment="dev"):
    app_config = get_config(environment)

    initialise_logger(
        app_config["application_settings"]["app_name"],
        level=app_config["application_settings"]["logging_level"],
        disable_file_handler=True
    )

    container = Container()
    container.config.from_dict(app_config)
    container.init_resources()
    logger.info(
        "startup complete: container initialized (env=%s, app=%s)",
        environment,
        app_config["application_settings"]["app_name"],
    )
    container.wire(
        modules=[
            maintenance, create, user, beets_import, rb_import_export, serato_import_export, export_progress, sync, track, sys.modules[__name__]
        ]
    )
    return container


def get_config(environment: str) -> dict:
    config_file_base = Path(__file__).parent / "config" / "config.base.yaml"
    conf_base = yaml.safe_load(config_file_base.read_text())
    config_file = Path(__file__).parent / "config" / f"config.{environment}.yaml"
    app_config = yaml.safe_load(config_file.read_text())
    app_config.update(conf_base)
    return app_config
