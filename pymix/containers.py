from pathlib import Path
import aiohttp

from toredocore.providers.healthcheck.async_healthcheck_provider import AsyncHealthcheckProvider
from toredocore.providers.healthcheck.healthcheck_dependency import HealthcheckDependency
from dependency_injector import containers, providers
from pyserato.crate import Builder

from pymix.clients.beets_client import BeetsClient
from pymix.clients.navidrome_client import NavidromeClient
from pymix.clients.subsonic_client import SubsonicClient
from pymix.controllers.db_controller import DbController
from pymix.controllers.rekordbox_xml_controller import RekordboxXMLController
from pymix.controllers.serato_controller import SeratoController
from pymix.factories.aiohttp_session_resource import init_aiohttp_session
from pymix.factories.create_db_session import create_db_session
from pymix.factories.rekordbox_xml_factory import RekordboxXMLFactory
from pymix.handlers.env_file_handler import DockerEnvFileHandler
from pymix.handlers.filebrowser_file_handler import FileBrowserFileHandler
from pymix.handlers.rb_backup_file_handler import RBBackupFileHandler
from pymix.handlers.serato_backup_file_handler import SeratoBackupFileHandler
from pymix.orchestrators.rekordbox_xml_orchestrator import RekordboxXMLOrchestrator
from pymix.orchestrators.serato_crate_orchestrator import SeratoCrateOrchestrator
from pymix.orchestrators.services_orchestrator import ServicesOrchestrator
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
        host=config.containers.subsonic.host,
        session=aiohttp_session,
        version=config.containers.subsonic.version,
        music_path_base_to_remove=config.containers.subsonic.music_path_base_to_remove,
        zip_name=config.rekordbox.zip_name,
        app_env=config.app_env
    )

    navidrome_client = providers.Singleton(
        NavidromeClient,
        host=config.containers.subsonic.host,
        session=aiohttp_session
    )

    rekordbox_xml_factory = providers.Factory(
        RekordboxXMLFactory,
        providers.Factory(
            Path,
            config.rekordbox.xml_path
        )
    )

    db = providers.Singleton(
        create_db_session,
        providers.Factory(
            Path,
            config.db.path
        )
    )

    db_controller = providers.Singleton(
        DbController,
        db,
        config.app_env
    )

    env_file_handler = providers.Singleton(
        DockerEnvFileHandler,
    )

    services_orchestrator = providers.Singleton(
        ServicesOrchestrator,
        db_controller,
        navidrome_client,
        env_file_handler,
        config
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
        config.containers.beets.data,
        config.containers.beets.data_public,
    )
    file_browser_file_handler = providers.Singleton(
        FileBrowserFileHandler,
        config.rekordbox.zip_name,
        config.containers.subsonic.serving_music_path_base,
        config.containers.filebrowser.data,
        config.containers.beets.data,
        config.containers.beets.data_public,
        config.update_job_period_s
    )

    rekordbox_xml_controller = providers.Singleton(
        RekordboxXMLController,
        subsonic_orchestrator,
        rekordbox_xml_orchestrator,
        rb_backup_file_handler,
        file_browser_file_handler,
        config.rekordbox.restored_rb_output_root
    )

    serato_crate_orchestrator = providers.Singleton(
        SeratoCrateOrchestrator,
        providers.Singleton(Builder)
    )
    serato_controller = providers.Singleton(
        SeratoController,
        subsonic_orchestrator,
        serato_crate_orchestrator,
        providers.Singleton(
            SeratoBackupFileHandler,
            config.containers.beets.data

        ),
        file_browser_file_handler,
    )

    beets_client = providers.Singleton(
        BeetsClient,
        host=config.containers.beets.host,
        session=aiohttp_session,
        app_env=config.app_env
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

