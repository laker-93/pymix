from pathlib import Path
import aiohttp

from toredocore.providers.healthcheck.async_healthcheck_provider import AsyncHealthcheckProvider
from toredocore.providers.healthcheck.healthcheck_dependency import HealthcheckDependency
from dependency_injector import containers, providers

from pymix.clients.subsonic_client import SubsonicClient
from pymix.controllers.rekordbox_xml_controller import RekordboxXMLFactory, RekordboxXMLController
from pymix.factories.aiohttp_session_resource import init_aiohttp_session


class Container(containers.DeclarativeContainer):
    config = providers.Configuration()

    aiohttp_session = providers.Resource(
        init_aiohttp_session,
        connector=providers.Factory(
            aiohttp.TCPConnector, verify_ssl=False
        )
    )

    subsonic_client = providers.Singleton(
        SubsonicClient,
        host=config.subsonic.host,
        session=aiohttp_session,
        username=config.subsonic.username,
        version=config.subsonic.version,
        music_path_base_to_add=config.subsonic.music_path_base_to_add,
        music_path_base_to_remove=config.subsonic.music_path_base_to_remove
    )


    rekordbox_xml_factory = providers.Factory(
        RekordboxXMLFactory,
        providers.Factory(
            Path,
            config.rekordbox.xml_path
        )
    )

    rekordbox_xml_controller = providers.Factory(
        RekordboxXMLController,
        subsonic_client,
        rekordbox_xml_factory
    )

    healthcheck_provider = providers.Resource(
        AsyncHealthcheckProvider,
        config.app_name,
        config.app_env,
        providers.List(
            providers.Factory(
                HealthcheckDependency,
                name='rekordbox xml controller',
                healthcheck_fn=rekordbox_xml_controller.provided.get_healthcheck,
                expected_return=True,
                key_to_check='is_healthy',
                capture_full_response=True
            ),
        )
    )

