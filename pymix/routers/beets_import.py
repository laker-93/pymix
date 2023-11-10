import logging
from pathlib import Path

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends

from pymix.containers import Container
from pymix.controllers.db_controller import DbController
from pymix.controllers.rekordbox_xml_controller import RekordboxXMLController

router = APIRouter()

logger = logging.getLogger(__name__)

@router.post("/beets_import", tags=["import"])
@inject
async def beets_import(
    session_id: str | None = None,
    username: str | None = None,
    rekordbox_xml_controller: RekordboxXMLController = Depends(Provide[Container.rekordbox_xml_controller]),
    db_controller: DbController = Depends(Provide[Container.db_controller]),
)-> dict:
    success = True
    reason = ""
    if not username and not session_id:
        success = False
        reason = "must have a username or session id to identify user"
    if not username and session_id:
        try:
            user = db_controller.get_user_by_session_id(session_id)
        except Exception as ex:
            logger.error(f'error occurred getting user for session id {session_id}', exc_info=True)
            reason = repr(ex)
        else:
            username = user['username']
    if username:
        logger.info(f'importing tracks for user {username}')
        try:
            await rekordbox_xml_controller.consume_from_filebrowser(username)
        except Exception as ex:
            success = False
            msg = f'error occurred importing the following path in to beets for user {username} {repr(ex)}'
            logger.error(msg, exc_info=True)
            reason = msg
    return {
        'success': success,
        'reason': reason
    }
