import logging
from http import HTTPStatus
from pathlib import Path
from typing import Optional

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, Cookie, Header
from pydantic import BaseModel
from starlette.responses import JSONResponse

from pymix.containers import Container
from pymix.orchestrators.services_orchestrator import ServicesOrchestrator
from pymix.controllers.db_controller import DbController, InvalidCredentialsError, InvalidTokenError
from pymix.routers.admin import require_admin_token
from pymix.routers.auth import require_username
from pymix.services import metrics

router = APIRouter()

logger = logging.getLogger(__name__)


class CreateUserRequest(BaseModel):
    username: str
    password: str
    email: str
    token: str

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
        session_id = await services_orchestrator.create(username, password, email, request.token)
    except InvalidTokenError as ex:
        # A client error, not a server one: the caller supplied a token that does not
        # exist or that somebody has already signed up with. Logged without a stack
        # trace, and answered with the exception's own message rather than `repr(ex)` --
        # this is the one create failure a stranger can reach, so it must not echo
        # internal detail back. It stays a 500 because that is the only failure status
        # the client's ts-rest contract declares for this route; see the PR for the
        # follow-up that makes it the 403 it should be, in lockstep with subbox-app.
        logger.warning(f'refused to create user {username}: {ex}')
        reason = str(ex)
        success = False
        # `rejected`, not `error`: a deliberate refusal, not something breaking. Kept
        # apart so a rise here reads as "invites are being replayed or mistyped"
        # rather than as pymix failing, which is a different thing to go and look at.
        metrics.observe_signup("rejected")
    except Exception as ex:
        logger.error(f'error occurred creating services for user', exc_info=True)
        reason = repr(ex)
        success = False
        metrics.observe_signup("error")
    if session_id is None:
        # The user cap was hit, so no account exists. This used to leave `success` True
        # and answer 200 with the reason in the body, i.e. report a signup that never
        # happened as a success -- the cap is 10, so a beta reaches it for real.
        reason = "max number of users reached"
        success = False
        # Also `rejected`: the cap working as designed. Separated from `error` so a
        # rise reads as "the beta is full and people are still arriving" -- a decision
        # to make, not a fault to fix.
        metrics.observe_signup("rejected")
    elif success:
        metrics.observe_signup("created")
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
    status_code = HTTPStatus.OK
    print(f'got session id {session_id}')
    if session_id is None or session_id == 'none':
        try:
            session_id = db_controller.create_session(username, password)
        except InvalidCredentialsError as ex:
            # A client error, not a server one. Log without a stack trace and answer
            # 401 with the generic message, never the underlying detail.
            logger.warning(f'failed login attempt for user {username}: invalid credentials')
            reason = str(ex)
            success = False
            status_code = HTTPStatus.UNAUTHORIZED
            metrics.observe_login("invalid_credentials")
        except Exception as ex:
            logger.error(f'error occured logging in user', exc_info=True)
            reason = repr(ex)
            success = False
            status_code = HTTPStatus.INTERNAL_SERVER_ERROR
            metrics.observe_login("error")
        else:
            metrics.observe_login("created")
    else:
        # The client already had a session and is only asking for the cookie back. Not
        # counted as a login attempt -- it never reached the password check -- but
        # counted, because otherwise a returning user is invisible here.
        metrics.observe_login("resumed")
    response = JSONResponse(content=reason, status_code=status_code)
    if success:
        logger.info(f'setting cookie to {session_id}')
        response.set_cookie(key='session_id', value=session_id, httponly=True, secure=True, samesite="none")
    return response


@router.get("/user/is_valid_token", tags=["user"])
@inject
async def library_size(
    token: str,
    db_controller: DbController = Depends(Provide[Container.db_controller]),
) -> dict:
    success = False
    is_valid_token = False
    reason = ""
    try:
        is_valid_token = db_controller.is_valid_token(token)
    except Exception as ex:
        logger.error(f'error occurred checking token {token}', exc_info=True)
        reason = repr(ex)
    else:
        success = True
    return {
        'success': success,
        'is_valid_token': is_valid_token,
        'reason': reason
    }


@router.get("/user/library_size", tags=["user"])
@inject
async def library_size(
        username: str = Depends(require_username),
) -> dict:
    total_size = sum(file.stat().st_size for file in Path(f'/private-music/{username}').rglob('*'))
    return {
        'success': True,
        'total_size_bytes': total_size,
        'reason': ""
    }


@router.get("/user/storage_check", tags=["user"])
@inject
async def storage_check(
        uploadSizeBytes: int = 0,
        session_id: str | None = Cookie(None),
        authorization: str | None = Header(None),
        db_controller: DbController = Depends(Provide[Container.db_controller]),
) -> dict:
    success = False
    exceeded = False
    reason = ""
    current_usage_bytes = 0
    max_storage_bytes = 0
    remaining_bytes = 0

    if uploadSizeBytes < 0:
        return {
            'allowed': False,
            'currentUsageBytes': 0,
            'maxStorageBytes': 0,
            'remainingBytes': 0,
            'reason': 'uploadSizeBytes must be >= 0',
            'success': False
        }

    # Prefer Bearer token, fall back to cookie for existing clients.
    auth_session_id = session_id
    if authorization:
        parts = authorization.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            auth_session_id = parts[1].strip()

    if not auth_session_id:
        return {
            'allowed': False,
            'currentUsageBytes': 0,
            'maxStorageBytes': 0,
            'remainingBytes': 0,
            'reason': 'must have an Authorization Bearer token or session cookie to identify user',
            'success': False
        }

    try:
        user = db_controller.get_user_by_session_id(auth_session_id)
        if not user:
            reason = 'no user found for provided session'
        else:
            username = user['username']
            exceeded, max_storage_bytes, current_usage_bytes = db_controller.user_library_size_exceeded(username, uploadSizeBytes)
            remaining_bytes = max(0, max_storage_bytes - current_usage_bytes)
            reason = 'storage limit exceeded' if exceeded else 'ok'
            success = True
    except Exception as ex:
        logger.error('error occurred performing storage check', exc_info=True)
        reason = repr(ex)

    return {
        'allowed': not exceeded,
        'currentUsageBytes': current_usage_bytes,
        'maxStorageBytes': max_storage_bytes,
        'remainingBytes': remaining_bytes,
        'reason': reason,
        'success': success
    }

# --- operator lookup helpers -------------------------------------------------
#
# These two resolve a *stranger's* row by a name or a session id, so neither can
# authenticate its caller the way every user-scoped route does (`require_user`
# reads the session cookie and answers about the holder, never about whoever was
# named). `routers/auth.py` says where that leaves them:
#
#   > `username` survives only where it is an *argument* rather than a claim of
#   > identity: creating a user, logging in, and the admin lookup helpers.
#
# They are the admin lookup helpers, and they were never gated. Until now an
# unauthenticated caller who guessed a username got back the full `user_table`
# row -- including the account password, which is stored in cleartext and opens
# pymix, the user's Navidrome and filebrowser alike (GHSA-hqhc-vv93-fhcv).

#: Columns that must never cross the API boundary, whoever is asking. Redacting
#: here rather than trusting the gate is deliberate: the token only says the
#: caller is an operator, and an operator's terminal, shell history, screen
#: share and log scraper are all places a password should still never reach. A
#: value that is never serialised cannot leak by accident later.
_SECRET_USER_FIELDS = ('password',)


def _without_secrets(user: Optional[dict]) -> dict:
    """The user row minus anything that must not be serialised."""
    if not user:
        return {}
    return {k: v for k, v in user.items() if k not in _SECRET_USER_FIELDS}


@router.get("/user/get_by_username", tags=["db"], dependencies=[Depends(require_admin_token)])
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
        'user': _without_secrets(user)
    }

@router.get("/user/get_by_session_id", tags=["db"], dependencies=[Depends(require_admin_token)])
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
        # Log the identity, never the row: `user` carries the cleartext password, and
        # an f-string of the whole dict wrote it into the application log on every
        # call -- a second copy of the secret, in a place with a different audience
        # and a much longer life than the response (GHSA-hqhc-vv93-fhcv).
        logger.info(f'found user {user.get("username") if user else None}')
        return {
            'success': success,
            'reason': reason,
            'user': _without_secrets(user)
        }
