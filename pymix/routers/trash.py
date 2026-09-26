import logging
from typing import Any, Dict

from dependency_injector.wiring import inject, Provide
from fastapi import APIRouter, Depends, HTTPException

from pymix.containers import Container
from pymix.controllers.db_controller import DbController
from pymix.routers.auth import require_uploader
from pymix.services.trash import ItemState, TrashService, batch_state

logger = logging.getLogger(__name__)
router = APIRouter()


def _summary(batch: dict) -> Dict[str, Any]:
    return {
        'batch_id': batch['batch_id'],
        'kind': batch['kind'],
        'label': batch['label'],
        'bytes': batch['bytes'],
        'deleted_at': batch['created_at'],
        'expires_at': batch['expires_at'],
        'state': batch_state(i['state'] for i in batch['items']),
        'items': [
            {
                'subbox_id': i['subbox_id'],
                'relative_path': i['relative_path'],
                'size': i['size'],
                'state': i['state'],
            }
            for i in batch['items']
        ],
    }


@router.get("/trash", tags=["trash"])
@inject
async def list_trash(
        user: dict = Depends(require_uploader),
        db_controller: DbController = Depends(Provide[Container.db_controller]),
) -> Dict[str, Any]:
    """The user's restorable trash batches, newest first (#200). A batch that has
    been purged or restored is history, and not listed."""
    batches = [_summary(b) for b in db_controller.get_trash_batches(user['username'])]
    return {
        'batches': [b for b in batches if b['state'] == ItemState.RESTORABLE.value],
        'trash_bytes': db_controller.trash_bytes(user['username']),
    }


@router.delete("/trash/{batch_id}", tags=["trash"])
@inject
async def purge_trash_batch(
        batch_id: str,
        user: dict = Depends(require_uploader),
        db_controller: DbController = Depends(Provide[Container.db_controller]),
        trash_service: TrashService = Depends(Provide[Container.trash_service]),
) -> Dict[str, Any]:
    """Destroy one batch now, instead of waiting for it to expire."""
    username = user['username']
    if db_controller.get_trash_batch(batch_id, username) is None:
        raise HTTPException(status_code=404, detail=f"no trash batch {batch_id}")
    outcome = await trash_service.purge_batch(batch_id, username)
    return {
        'success': not outcome.errors,
        'batch_id': batch_id,
        'n_purged': outcome.n_purged,
        'errors': outcome.errors,
    }


@router.delete("/trash", tags=["trash"])
@inject
async def empty_trash(
        user: dict = Depends(require_uploader),
        db_controller: DbController = Depends(Provide[Container.db_controller]),
        trash_service: TrashService = Depends(Provide[Container.trash_service]),
) -> Dict[str, Any]:
    """Destroy everything in the user's trash."""
    username = user['username']
    n_purged, errors = 0, []
    for batch in db_controller.get_trash_batches(username):
        if batch_state(i['state'] for i in batch['items']) not in (ItemState.RESTORABLE.value, ItemState.EXPIRED.value):
            continue
        outcome = await trash_service.purge_batch(batch['batch_id'], username)
        n_purged += outcome.n_purged
        errors.extend(outcome.errors)
    return {'success': not errors, 'n_purged': n_purged, 'errors': errors}
