from pathlib import Path
import aiohttp

from toredocore.providers.healthcheck.async_healthcheck_provider import AsyncHealthcheckProvider
from toredocore.providers.healthcheck.healthcheck_dependency import HealthcheckDependency
from dependency_injector import containers, providers

from pymix.clients.subsonic_client import SubsonicClient
from pymix.controllers.db_controller import DbController
from pymix.controllers.rekordbox_xml_controller import RekordboxXMLFactory, RekordboxXMLController
from pymix.factories.aiohttp_session_resource import init_aiohttp_session
from pymix.factories.create_db_session import create_db_session
from pymix.handlers.filebrowser_file_handler import FileBrowserFileHandler
from pymix.handlers.rb_backup_file_handler import RBBackupFileHandler
from pymix.orchestrators.rekordbox_xml_orchestrator import RekordboxXMLOrchestrator
from pymix.orchestrators.subsonic_orchestrator import SubsonicOrchestrator


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

    db = providers.Factory(
        create_db_session,
        providers.Factory(
            Path,
            config.db.path
        )
    )

    db_controller = providers.Singleton(
        DbController,
        db
    )

    subsonic_orchestrator = providers.Singleton(
        SubsonicOrchestrator,
        subsonic_client
    )

    rekordbox_xml_orchestrator = providers.Singleton(
        RekordboxXMLOrchestrator,
        rekordbox_xml_factory
    )
    rb_backup_file_handler = providers.Singleton(
        RBBackupFileHandler,
        rekordbox_xml_orchestrator,
        config.beets.data
    )
    file_browser_file_handler = providers.Singleton(
        FileBrowserFileHandler,
        config.filebrowser.data,
        config.beets.data
    )

    rekordbox_xml_controller = providers.Singleton(
        RekordboxXMLController,
        subsonic_orchestrator,
        rekordbox_xml_orchestrator,
        rb_backup_file_handler,
        file_browser_file_handler,
        config.rekordbox.restored_rb_output_root
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

