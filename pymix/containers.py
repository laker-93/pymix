from toredocore.database.database_session_manager import DatabaseSessionManager
from toredocore.database.generic_database_gateway import GenericDatabaseGateway
from toredocore.providers.healthcheck.async_healthcheck_provider import AsyncHealthcheckProvider
from toredocore.providers.healthcheck.healthcheck_dependency import HealthcheckDependency
from dependency_injector import containers, providers
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from pymix.clients.navidrome_client import NavidromeClient
from pymix.controllers.db_store_controller import DbStoreController
from pymix.db_model.job_database_gateway import JobDatabaseGateway
from pymix.factories.db_session_factory import DbSession
from pymix.factories.aiohttp_session_resource import init_aiohttp_session
from pymix.utils.scheduled_jobs import ScheduledJobs
from pymix.utils.scheduled_tasks import ScheduledTasks


class Container(containers.DeclarativeContainer):
    config = providers.Configuration()

    aiohttp_session = providers.Resource(init_aiohttp_session)

    navidrome_client = providers.Factory(
        NavidromeClient,
        host=config.navidrome.host,
        session=aiohttp_session,
        username=config.navidrome.username,
        version=config.navidrome.version,
    )

    # TODO the JobDataBaseGateway in toredcore uses the GenericDatabaseGateway which is non async
    # TODO compatible. For now, use a synchronous db engine to avoid having to change this code.
    # TODO support async db gateway in toredocore. Then remove the synchronous db engine here.
    db_engine = providers.Resource(
        create_engine, config.db_settings.sync_conn_string, echo=config.db_settings.db_engine_echo
    )
    session_factory = providers.Resource(
        sessionmaker, bind=db_engine, expire_on_commit=False
    )

    database_session_manager = providers.Resource(
        DatabaseSessionManager, session_factory
    )

    db_session = providers.Factory(
        DbSession,
        app_config=config
    )

    db_controller = providers.Singleton(
        DbStoreController,
        app_configuration=config,
        db_session=db_session
    )

    job_database_gateway = providers.Resource(
        JobDatabaseGateway, config.APP_NAME, providers.Resource(
            GenericDatabaseGateway, database_session_manager, "job")
    )

    scheduled_jobs = providers.Singleton(
        ScheduledJobs, db_controller
    )

    scheduled_tasks = providers.Resource(
        ScheduledTasks, scheduled_jobs, job_database_gateway
    )


    healthcheck_provider = providers.Resource(
        AsyncHealthcheckProvider,
        config.app_name,
        config.app_env,
        providers.List(
            providers.Factory(
                HealthcheckDependency,
                name='db controller',
                healthcheck_fn=db_controller.provided.get_healthcheck,
                expected_return=True,
                key_to_check='is_healthy',
                capture_full_response=True
            ),
        )
    )