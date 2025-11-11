import asyncio

from pathlib import Path
from pymix.controllers.rekordbox_xml_controller import RekordboxXMLController
from pymix.controllers.serato_controller import SeratoController
from pymix.orchestrators.serato_crate_orchestrator import SeratoCrateOrchestrator
from pymix.registration import create_app, create_container


async def create_nav_from_crates(serato_controller: SeratoController, user: dict, serato_crate_path: Path, audio_files_to_import: Path):
    return await serato_controller.create_subsonic_playlists_from_crates(user, serato_crate_path, audio_files_to_import)

async def create_crates_from_nav(serato_controller: SeratoController, user_root: str, user: dict, output_path: Path):
    await serato_controller.create_crates_from_subsonic_playlists(user_root, user, output_path)

async def consume_from_filebrowser(rekordbox_xml_controller: RekordboxXMLController):
    await rekordbox_xml_controller.consume_from_filebrowser()

def get_subbox_playlists_from_crates(serato_crate_orchestrator: SeratoCrateOrchestrator, path: Path):
    serato_crate_orchestrator.get_subbox_playlists_from_crates(path)

async def main():
    user = {
        'username': 'emc',
        'password': 'emc',
    }
    container = create_container('dev')
    container.wire(modules=[__name__])
    orchestrator = container.serato_crate_orchestrator()
    get_subbox_playlists_from_crates(orchestrator, Path('/Users/lukepurnell/Music/_Serato_/SubCrates.zip'))
    #controller = await container.serato_controller()
    #result = await create_crates_from_nav(controller, '/Users/lukepurnell/subbox/emc/nav_music', user,  DEFAULT_SERATO_FOLDER)
    #result = await create_nav_from_crates(
    #    controller,
    #    user,
    #    Path('/Users/lukepurnell/Music/_Serato_/Subcrates'),
    #    Path('/Users/lukepurnell/serato/test_serato_to_nav')
    #)
    #await consume_from_filebrowser(controller)

if __name__ == "__main__":
    asyncio.run(main())
