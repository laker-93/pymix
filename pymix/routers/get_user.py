import logging

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends

from pymix.containers import Container
from pymix.controllers.db_controller import DbController

router = APIRouter()

logger = logging.getLogger(__name__)

@router.get("/get_user", tags=["db"])
@inject
async def get_user(
        username: str,
        db_controller: DbController = Depends(Provide[Container.db_controller]),
)-> dict:
    logger.info(f'retrieving user {username}')
    reason = ""
    success = True
    user = {}
    try:
        user = db_controller.get_user(username)
    except Exception as ex:
        logger.error(f'error occurred getting user {username}', exc_info=True)
        reason = repr(ex)
        success = False
    return {
        'success': success,
        'reason': reason,
        'user': user
    }
