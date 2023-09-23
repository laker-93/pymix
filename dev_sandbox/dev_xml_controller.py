import asyncio

from pathlib import Path
from dependency_injector.wiring import inject, Provide

from pymix.containers import Container
from pymix.clients.subsonic_client import SubsonicClient
from pymix.controllers.rekordbox_xml_controller import RekordboxXMLController
from pymix.registration import create_app, create_container


async def create_nav_from_xml(rekordbox_xml_controller: RekordboxXMLController, rekordbox_xml_path: Path, audio_files_to_import: Path):
    return await rekordbox_xml_controller.create_subsonic_playlists_from_xml(rekordbox_xml_path, audio_files_to_import)

async def create_xml_from_nav(rekordbox_xml_controller: RekordboxXMLController, xml_path: Path, xml_output_path: Path):
    await rekordbox_xml_controller.create_rekordbox_xml_from_subsonic_playlists(xml_path, xml_output_path)

async def main():
    container = create_container('dev')
    container.wire(modules=[__name__])
    controller = await container.rekordbox_xml_controller()
    result = await create_nav_from_xml(controller, Path('rekordbox_import/rekordbox-input.xml'), Path('rekordbox_import/rekordbox_bak'))

if __name__ == "__main__":
    asyncio.run(main())
