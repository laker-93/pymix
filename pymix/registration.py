import argparse
import os
import yaml
import sys
from pathlib import Path

from toredocore.logger import initialise_logger
from fastapi import FastAPI

from pymix import constants
from pymix.containers import Container
from pymix.routers import maintenance
from pymix.utils import scheduled_jobs, scheduled_tasks
from pymix.utils.utility import get_project_root


def register_app(environment=None):
    if not environment:
        parser = argparse.ArgumentParser()
        parser.add_argument("-e", "--environment", default="dev")
        args = parser.parse_args()
        environment = args.environment
    app_config = get_config(environment)

    initialise_logger(
        app_config["application_settings"]["app_name"],
        level=app_config["application_settings"]["logging_level"],
        disable_file_handler=True
    )

    app = FastAPI(
        title=constants.title, version=constants.version, description=constants.description
    )

    app.include_router(maintenance.router)
    container = Container()
    container.config.from_dict(app_config)
    container.init_resources()
    container.wire(modules=[maintenance, scheduled_jobs, scheduled_tasks, sys.modules[__name__]])

    app.container = container
    return app, app_config


def get_config(environment: str) -> dict:
    config_file_base = Path(__file__).parent / "config" / "config.base.yaml"
    conf_base = yaml.safe_load(config_file_base.read_text())
    config_file = Path(__file__).parent / "config" / f"config.{environment}.yaml"
    app_config = yaml.safe_load(config_file.read_text())
    app_config.update(conf_base)
    return app_config
