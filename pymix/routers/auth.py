"""Session-cookie authentication for the pymix API.

Every user-scoped endpoint resolves its caller through `require_user` (or
`require_username`, when only the name is needed).

Endpoints used to accept an explicit `username` query/body param as an alternative to
the cookie. That param was never verified against anything — any caller could act as
any user simply by naming them — so the `session_id` cookie set by `/user/create` and
`/user/login` is now the only accepted identity for user-scoped routes.

`username` survives only where it is an *argument* rather than a claim of identity:
creating a user, logging in, and the admin lookup helpers.
"""

import logging
from typing import Optional

from dependency_injector.wiring import Provide, inject
from fastapi import Cookie, Depends, HTTPException

from pymix.containers import Container
from pymix.controllers.db_controller import DbController

logger = logging.getLogger(__name__)

# Deliberately uniform: never leak whether the session was absent, unknown or expired.
_UNAUTHENTICATED = "Not authenticated: a valid session_id cookie is required."


@inject
def require_user(
    session_id: Optional[str] = Cookie(None),
    db_controller: DbController = Depends(Provide[Container.db_controller]),
) -> dict:
    """Return the calling user's row, or raise 401.

    401 (not 400/404/500) is what the subbox-app client's reauth interceptor keys off
    to silently refresh a lapsed cookie and replay the request, so every failure to
    identify the caller — missing, unknown, or expired session — must surface as 401.
    """
    if not session_id:
        raise HTTPException(status_code=401, detail=_UNAUTHENTICATED)

    try:
        user = db_controller.get_user_by_session_id(session_id)
    except Exception:
        # get_user_by_session_id raises on a duplicated session_id; treat any lookup
        # failure as unauthenticated rather than letting it surface as a 500.
        logger.error("error resolving user for session id %s", session_id, exc_info=True)
        raise HTTPException(status_code=401, detail=_UNAUTHENTICATED)

    if not user:
        # Unknown/expired sessions come back as None rather than raising.
        logger.info("no user found for session id %s", session_id)
        raise HTTPException(status_code=401, detail=_UNAUTHENTICATED)

    return user


def require_username(user: dict = Depends(require_user)) -> str:
    """Return just the calling user's username, or raise 401."""
    return user["username"]


# `demo` is a public trial login with no per-user container stack of its own — its
# Navidrome browsing identity already lives inside demoadmin's Navidrome container,
# so its pymix-side reads are proxied there too rather than resolving containers
# that don't exist.
DEMO_USERNAME = "demo"
DEMO_PROXY_USERNAME = "demoadmin"


@inject
def require_reader(
    user: dict = Depends(require_user),
    db_controller: DbController = Depends(Provide[Container.db_controller]),
) -> dict:
    """Like require_user, but resolves `demo` to demoadmin's row.

    Use on routes that only read a user's library (sync/export): every downstream
    client derives its container target from this dict, so substituting it here is
    enough to make `demo`'s reads target demoadmin's containers.
    """
    if user["username"] == DEMO_USERNAME:
        return db_controller.get_user(DEMO_PROXY_USERNAME)
    return user


def require_uploader(user: dict = Depends(require_user)) -> dict:
    """Like require_user, but blocks `demo` with a 403.

    Use on routes that mutate a user's library (upload/import) — `demo` may read
    demoadmin's library (via require_reader) but must never be able to write to it.
    """
    if user["username"] == DEMO_USERNAME:
        raise HTTPException(status_code=403, detail="demo account cannot upload or import")
    return user
