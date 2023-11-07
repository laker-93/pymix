import asyncio

from pathlib import Path
from dependency_injector.wiring import inject, Provide

from pymix.containers import Container
from pymix.clients.subsonic_client import SubsonicClient
from pymix.controllers.rekordbox_xml_controller import RekordboxXMLController
from pymix.registration import create_app, create_container



async def main():
    container = create_container('dev')
    container.wire(modules=[__name__])
    controller = container.db_controller()
    controller.create_session('emc', 'emc')


if __name__ == "__main__":
    asyncio.run(main())
