import logging
from pathlib import Path
from typing import Optional

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends

from pymix.containers import Container
from pymix.controllers.rekordbox_xml_controller import RekordboxXMLController

router = APIRouter()

logger = logging.getLogger(__name__)

@router.get("/create_xml", tags=["RekordBoxXML"])
@inject
async def create_xml(
        xml_path: Path,
        xml_output_path: Path,
        rekordbox_xml_controller: RekordboxXMLController = Depends(Provide[Container.rekordbox_xml_controller])
)-> dict:
    logger.info(f'creating rekordbox xml')
    await rekordbox_xml_controller.create_rekordbox_xml_from_subsonic_playlists(xml_path, xml_output_path)
    return {
        'success': True
    }
