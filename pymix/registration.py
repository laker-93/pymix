import yaml
import sys
from pathlib import Path

from toredocore.logger import initialise_logger
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from pymix import constants
from pymix.containers import Container
from pymix.routers import maintenance, create, user, beets_import, rb_import_export, serato_import_export, export_progress, sync


def create_app():
    app = FastAPI(
        title=constants.title, version=constants.version, description=constants.description,
        # required for dev testing
        root_path="/pymix"
    )
    app.add_middleware(
        CORSMiddleware,
        # need to set explicit origins here rather than * since feishin will be sending credentials in cookies:
        # https://stackoverflow.com/questions/18642828/origin-origin-is-not-allowed-by-access-control-allow-origin
        allow_origins=["http://localhost:4343", "https://player.sub-box.net/player", "http://localhost"],
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
    container.wire(
        modules=[
            maintenance, create, user, beets_import, rb_import_export, serato_import_export, export_progress, sync, sys.modules[__name__]
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
