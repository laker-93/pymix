import logging

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends

from pymix.containers import Container
from pymix.controllers.db_controller import DbController

router = APIRouter()

logger = logging.getLogger(__name__)

@router.get("/create_user", tags=["db"])
@inject
async def create_user(
        username: str,
        password: str,
        db_controller: DbController = Depends(Provide[Container.db_controller]),
)-> dict:
    logger.info(f'creating user {username}')
    reason = ""
    success = True
    try:
        db_controller.create_user(username, password)
    except Exception as ex:
        logger.error(f'error occurred creating user', exc_info=True)
        reason = repr(ex)
        success = False
    return {
        'success': success,
        'reason': reason
    }
