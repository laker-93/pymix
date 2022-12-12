from toredocore.database.database_session_manager import DatabaseSessionManager
from toredocore.database.generic_database_gateway import GenericDatabaseGateway
from toredocore.providers.healthcheck.async_healthcheck_provider import AsyncHealthcheckProvider
from toredocore.providers.healthcheck.healthcheck_dependency import HealthcheckDependency
from dependency_injector import containers, providers

from pymix.clients.subsonic_client import SubsonicClient
from pymix.factories.aiohttp_session_resource import init_aiohttp_session


class Container(containers.DeclarativeContainer):
    config = providers.Configuration()

    aiohttp_session = providers.Resource(init_aiohttp_session)

    navidrome_client = providers.Factory(
        SubsonicClient,
        host=config.navidrome.host,
        session=aiohttp_session,
        username=config.navidrome.username,
        version=config.navidrome.version,
    )
