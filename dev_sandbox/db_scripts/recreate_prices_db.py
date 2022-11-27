import asyncio

from toredocore import configuration_manager
from sqlalchemy.ext.asyncio import create_async_engine

from pymix.db_model.Price import Price
from pymix.utils.utility import get_project_root
from pymix import constants
import os


async def recreate_price_table():
    environment = "devlocal"
    config_folder = os.path.join(get_project_root(), constants.app_name, "config")
    app_config = configuration_manager.get_config(config_folder, environment)

    config = app_config[constants.db_settings]
    conn_string = config['conn_string']

    engine = create_async_engine(
        conn_string, echo=True,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Price.metadata.drop_all)
        await conn.run_sync(Price.metadata.create_all)

    # for AsyncEngine created in function scope, close and
    # clean-up pooled connections
    await engine.dispose()


if __name__ == '__main__':
    # WARNING RUNNING THIS SCRIPT WILL DELETE THE PRICES TABLE
    asyncio.run(recreate_price_table())
