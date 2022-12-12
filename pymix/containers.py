from pathlib import Path

from toredocore.database.database_session_manager import DatabaseSessionManager
from toredocore.database.generic_database_gateway import GenericDatabaseGateway
from toredocore.providers.healthcheck.async_healthcheck_provider import AsyncHealthcheckProvider
from toredocore.providers.healthcheck.healthcheck_dependency import HealthcheckDependency
from dependency_injector import containers, providers

from pymix.clients.rekordbox_client import RekordboxClient
from pymix.clients.subsonic_client import SubsonicClient
from pymix.controllers.playlist_controller import PlaylistController
from pymix.factories.aiohttp_session_resource import init_aiohttp_session


class Container(containers.DeclarativeContainer):
    config = providers.Configuration()

    aiohttp_session = providers.Resource(init_aiohttp_session)

    subsonic_client = providers.Factory(
        SubsonicClient,
        host=config.subsonic.host,
        session=aiohttp_session,
        username=config.subsonic.username,
        version=config.subsonic.version,
    )

    rekordbox_client = providers.Factory(
        RekordboxClient,
        providers.Factory(
            Path,
            config.rekordbox.xml_path
        )
    )

    playlist_controller = providers.Factory(
        PlaylistController,
        subsonic_client,
        rekordbox_client
    )

    healthcheck_provider = providers.Resource(
        AsyncHealthcheckProvider,
        config.app_name,
        config.app_env,
        providers.List(
            providers.Factory(
                HealthcheckDependency,
                name='db controller',
                healthcheck_fn=playlist_controller.provided.get_healthcheck,
                expected_return=True,
                key_to_check='is_healthy',
                capture_full_response=True
            ),
        )
    )

