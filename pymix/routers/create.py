import logging
from pathlib import Path

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends

from pymix.containers import Container

router = APIRouter()

logger = logging.getLogger(__name__)

@router.get("/create", tags=["setup"])
@inject
async def create(
        xml_path: Path,
        rekordbox_xml_controller: RekordboxXMLController = Depends(Provide[Container.rekordbox_xml_controller])
)-> dict:
    logger.info(f'creating rekordbox xml')
    await rekordbox_xml_controller.create_subsonic_playlists_from_xml(xml_path)
    return {
        'success': True
    }
