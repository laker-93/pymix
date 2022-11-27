import asyncio
import logging
from distutils.util import strtobool

from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from pymix import constants

logger = logging.getLogger(__name__)



class DbSession:
    def __init__(self, app_config):
        config = app_config[constants.db_settings]
        conn_string = config['conn_string']
        logger.info(f"connecting to {conn_string}")
        sql_alchemy_logging = bool(strtobool(config['sql_alchemy_logging'].lower()))
        engine = create_async_engine(conn_string, echo=sql_alchemy_logging)
        self.session = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
