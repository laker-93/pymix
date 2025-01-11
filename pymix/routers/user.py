import logging
from http import HTTPStatus
from pathlib import Path
from typing import Optional

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, Cookie
from pydantic import BaseModel
from starlette.responses import JSONResponse

from pymix.containers import Container
from pymix.orchestrators.services_orchestrator import ServicesOrchestrator
from pymix.controllers.db_controller import DbController

router = APIRouter()

logger = logging.getLogger(__name__)


class CreateUserRequest(BaseModel):
    username: str
    password: str
    email: str

class LoginUserRequest(BaseModel):
    username: str
    password: str
    session_id: Optional[str] = None

@router.post("/user/create", tags=["db"])
@inject
async def create_user(
        request: CreateUserRequest,
        services_orchestrator: ServicesOrchestrator = Depends(Provide[Container.services_orchestrator]),
)-> JSONResponse:

    username = request.username
    password = request.password
    email = request.email
    logger.info(f'creating user {username}')
    reason = ""
    success = True
    session_id = ""
    try:
        session_id = await services_orchestrator.create(username, password, email)
    except Exception as ex:
        logger.error(f'error occurred creating services for user', exc_info=True)
        reason = repr(ex)
        success = False
    if session_id is None:
        reason = "max number of users reached"
    response = JSONResponse(content=reason, status_code=HTTPStatus.OK if success else HTTPStatus.INTERNAL_SERVER_ERROR)
    if success and session_id:
        logger.info(f'setting cookie to {session_id}')
        response.set_cookie(key='session_id', value=session_id, httponly=True, secure=True, samesite="none")
    return response


@router.post("/user/login", tags=["user"])
@inject
async def user_login(
        request: LoginUserRequest,
        db_controller: DbController = Depends(Provide[Container.db_controller]),
)-> JSONResponse:
    username = request.username
    password = request.password
    session_id = request.session_id
    logger.info(f'logging in user {username}')
    reason = ""
    success = True
    print(f'got session id {session_id}')
    if session_id is None or session_id == 'none':
        try:
            session_id = db_controller.create_session(username, password)
        except Exception as ex:
            logger.error(f'error occured logging in user', exc_info=True)
            reason = repr(ex)
            success = False
    response = JSONResponse(content=reason, status_code=HTTPStatus.OK if success else HTTPStatus.INTERNAL_SERVER_ERROR)
    if success:
        logger.info(f'setting cookie to {session_id}')
        response.set_cookie(key='session_id', value=session_id, httponly=True, secure=True, samesite="none")
    return response


@router.get("/user/library_size", tags=["user"])
@inject
async def library_size(
        username: str | None = None,
        session_id: str | None = Cookie(None),
        db_controller: DbController = Depends(Provide[Container.db_controller]),
) -> dict:
    success = False
    total_size = 0
    reason = ""
    if not session_id:
        reason = "must have a session id to identify user"
    if session_id:
        try:
            user = db_controller.get_user_by_session_id(session_id)
        except Exception as ex:
            logger.error(f'error occurred getting user for session id {session_id}', exc_info=True)
            reason = repr(ex)
        else:
            username = user['username']
            total_size = sum(file.stat().st_size for file in Path(f'/private-music/{username}').rglob('*'))
            success = True
    return {
        'success': success,
        'total_size_bytes': total_size,
        'reason': reason
    }

@router.get("/user/delete", tags=["db"])
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

@router.get("/user/get_by_username", tags=["db"])
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

@router.get("/user/get_by_session_id", tags=["db"])
@inject
async def get_user_by_session_id(
        session_id: str,
        db_controller: DbController = Depends(Provide[Container.db_controller]),
)-> dict:
    logger.info(f'retrieving user with session id {session_id}')
    reason = ""
    success = True
    user = {}
    try:
        user = db_controller.get_user_by_session_id(session_id)
    except Exception as ex:
        logger.error(f'error occurred getting user for session id {session_id}', exc_info=True)
        reason = repr(ex)
        success = False
    finally:
        logger.info(f'found user {user}')
        return {
            'success': success,
            'reason': reason,
            'user': user
        }
