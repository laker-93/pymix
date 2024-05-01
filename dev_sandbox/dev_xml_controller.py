import asyncio

from pathlib import Path
from dependency_injector.wiring import inject, Provide

from pymix.containers import Container
from pymix.clients.subsonic_client import SubsonicClient
from pymix.controllers.rekordbox_xml_controller import RekordboxXMLController
from pymix.registration import create_app, create_container


async def create_nav_from_xml(rekordbox_xml_controller: RekordboxXMLController, rekordbox_xml_path: Path, audio_files_to_import: Path):
    return await rekordbox_xml_controller.create_subsonic_playlists_from_xml(rekordbox_xml_path, audio_files_to_import)

async def create_xml_from_nav(rekordbox_xml_controller: RekordboxXMLController, user_root: str, user: dict, xml_path, xml_output_path: Path):
    await rekordbox_xml_controller.create_rekordbox_xml_from_subsonic_playlists(user_root, user, xml_path, xml_output_path)

async def consume_from_filebrowser(rekordbox_xml_controller: RekordboxXMLController):
    await rekordbox_xml_controller.consume_from_filebrowser()

async def main():
    user = {
        'username': 'emc',
        'password': 'emc',
    }
    container = create_container('dev')
    container.wire(modules=[__name__])
    controller = await container.rekordbox_xml_controller()
    result = await create_xml_from_nav(controller, '/Users/lukepurnell/subbox/emc/nav_music', user,  None, Path('subbox_export_rekordbox.xml'))
    #result = await create_nav_from_xml(controller, Path('/Users/lajp/rekordbox/rekordbox_081023.xml'), Path('/Users/lajp/rekordbox/rekordbox_bak'))
    #await consume_from_filebrowser(controller)

if __name__ == "__main__":
    asyncio.run(main())
