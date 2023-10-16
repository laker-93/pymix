import logging

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends

from pymix.containers import Container
from pymix.controllers.db_controller import DbController

router = APIRouter()

logger = logging.getLogger(__name__)

@router.get("/delete_user", tags=["db"])
@inject
async def delete_user(
        username: str,
        db_controller: DbController = Depends(Provide[Container.db_controller]),
)-> dict:
    logger.info(f'deleting user {username}')
    reason = ""
    success = True
    try:
        db_controller.delete_user(username)
    except Exception as ex:
        logger.error(f'error occurred deleting user', exc_info=True)
        reason = repr(ex)
        success = False
    return {
        'success': success,
        'reason': reason
    }
