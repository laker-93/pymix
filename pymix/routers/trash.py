import logging
from typing import Any, Dict

from dependency_injector.wiring import inject, Provide
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from pymix.containers import Container
from pymix.controllers.db_controller import DbController
from pymix.routers.auth import require_uploader
from pymix.services.import_progress import ImportProgressReporter, failure_reason
from pymix.services.trash import ItemState, TrashKind, TrashService, batch_state

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


@router.post("/trash/{batch_id}/restore", tags=["trash"])
@inject
async def restore_trash_batch(
        batch_id: str,
        background_tasks: BackgroundTasks,
        user: dict = Depends(require_uploader),
        db_controller: DbController = Depends(Provide[Container.db_controller]),
        trash_service: TrashService = Depends(Provide[Container.trash_service]),
) -> Dict[str, Any]:
    """
    Put a track batch back (#209). A job, because it waits on a library scan: poll
    GET /trash/restore/progress with the job_id. `nodes` and `playlist_entries`
    batches restore synchronously, and arrive with #207 and #208.
    """
    username = user['username']
    batch = db_controller.get_trash_batch(batch_id, username)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"no trash batch {batch_id}")
    if batch['kind'] != TrashKind.TRACK.value:
        raise HTTPException(status_code=400, detail=f"cannot restore a {batch['kind']} batch yet")
    n_restorable = sum(1 for i in batch['items'] if i['state'] == ItemState.RESTORABLE.value)
    if n_restorable == 0:
        raise HTTPException(status_code=409, detail="nothing in this batch can be restored")
    # One job per user at a time, as for imports: the job table asserts it.
    if db_controller.get_number_of_jobs(username, in_progress=True):
        raise HTTPException(status_code=409, detail="another job is running; try again when it finishes")
    job_id = db_controller.create_restore_job(username, n_restorable)
    background_tasks.add_task(_run_restore, trash_service, db_controller, batch_id, username, job_id)
    return {'success': True, 'job_id': job_id, 'n_tracks': n_restorable}


async def _run_restore(trash_service, db_controller, batch_id: str, username: str, job_id: str):
    reporter = ImportProgressReporter(db_controller, job_id)
    try:
        outcome = await trash_service.restore_tracks(batch_id, username, reporter)
    except Exception as ex:
        logger.error(f'restore of trash batch {batch_id} for {username} failed', exc_info=True)
        outcome = reporter.verdict(failure_reason(ex))
    db_controller.job_completed(job_id, outcome)


@router.get("/trash/restore/progress", tags=["trash"])
@inject
async def restore_progress(
        job_id: str,
        user: dict = Depends(require_uploader),
        db_controller: DbController = Depends(Provide[Container.db_controller]),
) -> Dict[str, Any]:
    """Where a restore job is, and when it has finished, what it did: `phases`
    counts each pass, and `warnings` names each track that came back without
    something it had (a star, a rating, its playlist entries)."""
    job = db_controller.get_job_by_id(user['username'], job_id)
    if job.get('name') != 'restore':
        raise HTTPException(status_code=404, detail=f"no restore job {job_id}")
    return {
        'job_id': job_id,
        'in_progress': job['in_progress'],
        'result': job['result'],
        'reason': job.get('reason') or '',
        'warnings': job.get('warnings') or '',
        'phase': job.get('phase'),
        'phase_n_processed': job.get('phase_n_processed') or 0,
        'phase_n_total': job.get('phase_n_total') or 0,
        'phases': job.get('phases'),
    }
