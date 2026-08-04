"""Operator-only endpoints for infra maintenance — not part of the client-facing API.

Gated by a shared secret (`PYMIX_ADMIN_TOKEN`) read straight from the environment,
the same way every other secret in this app is handled (see
`factories/create_db_session.py`) rather than through the YAML config. There is no
admin-role concept in the user model, so this is the minimal gate that stops the
public API from letting anyone recreate any user's beets container.
"""
import logging
import os
import secrets

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, Header, HTTPException

from pymix.containers import Container
from pymix.orchestrators.services_orchestrator import ServicesOrchestrator

router = APIRouter(prefix="/admin", tags=["Admin"])

logger = logging.getLogger(__name__)

_UNAUTHORIZED = "Not authorized: a valid X-Admin-Token header is required."


def require_admin_token(x_admin_token: str = Header(default=None)) -> None:
    expected = os.environ.get("PYMIX_ADMIN_TOKEN")
    if not expected:
        # Fail closed: an unset token must never be treated as "no auth required".
        logger.error("PYMIX_ADMIN_TOKEN is not set; refusing all admin requests")
        raise HTTPException(status_code=503, detail="Admin endpoints are not configured.")
    if not x_admin_token or not secrets.compare_digest(x_admin_token, expected):
        raise HTTPException(status_code=401, detail=_UNAUTHORIZED)


@router.get("/beets/{username}/status", dependencies=[Depends(require_admin_token)])
@inject
async def beets_status(
        username: str,
        services_orchestrator: ServicesOrchestrator = Depends(Provide[Container.services_orchestrator]),
) -> dict:
    """Read-only: current beet version/plugins, stats, and a one-track subbox_id
    spot check for this user's beets container. No lock, no mutation — supports
    auditing what a user's container is actually running before migrating it (#76).
    """
    logger.info(f"admin: beets status requested for {username}")
    try:
        return services_orchestrator.beets_status(username)
    except ValueError as ex:
        raise HTTPException(status_code=404, detail=str(ex))
    except AssertionError:
        raise HTTPException(status_code=404, detail=f"no such user: {username}")


@router.post("/beets/{username}/migrate", dependencies=[Depends(require_admin_token)])
@inject
async def migrate_beets(
        username: str,
        services_orchestrator: ServicesOrchestrator = Depends(Provide[Container.services_orchestrator]),
) -> dict:
    """Re-render this user's beets config from the current template and recreate
    their beets container so it picks up the pinned image (#76). Explicit,
    per-user, safe to re-run.
    """
    logger.info(f"admin: beets migration requested for {username}")
    try:
        return await services_orchestrator.migrate_beets_container(username)
    except ValueError as ex:
        raise HTTPException(status_code=404, detail=str(ex))
    except AssertionError:
        raise HTTPException(status_code=404, detail=f"no such user: {username}")
