import logging
from pathlib import Path

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends

from pymix.containers import Container
from pymix.controllers.rekordbox_xml_controller import RekordboxXMLController

router = APIRouter()

logger = logging.getLogger(__name__)

@router.get("/beets_import", tags=["import"])
@inject
async def create_subsonic_from_xml(
        username: str,
        rekordbox_xml_controller: RekordboxXMLController = Depends(Provide[Container.rekordbox_xml_controller])
)-> dict:
    logger.info(f'creating rekordbox xml')
    filebrowser_path = f'/Users/lukepurnell/subbox/docker-compose/filebrowser/data/users/{username}'
    audio_files_to_import = Path(filebrowser_path)
    await rekordbox_xml_controller.import_to_beets(audio_files_to_import)
    return {
        'success': True
    }
