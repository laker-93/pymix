import logging
from pathlib import Path
from typing import Optional

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends

from pymix.containers import Container
from pymix.controllers.rekordbox_xml_controller import RekordboxXMLFactory

router = APIRouter()

logger = logging.getLogger(__name__)

@router.get("/create_xml", tags=["RekordBoxXML"])
@inject
async def create_xml(
        xml_path: Optional[Path] = None,
        rekordbox_xml_controller: RekordboxXMLFactory = Depends(Provide[Container.rekordbox_xml_controller])
)-> dict:
    logger.info(f'creating rekordbox xml')
    rekordbox_xml_controller
    return {
        'success': True
    }
