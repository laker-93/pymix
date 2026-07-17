import logging
from typing import Dict

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel

from pymix.clients.beets_client import BeetsClient
from pymix.containers import Container
from pymix.controllers.db_controller import DbController
from pymix.controllers.rekordbox_xml_controller import RekordboxXMLController
from pymix.handlers.filebrowser_file_handler import FileBrowserFileHandler
from pymix.routers.auth import require_user, require_username

router = APIRouter()

logger = logging.getLogger(__name__)


class BeetsImportRequest(BaseModel):
    public: bool


router = APIRouter()


@router.delete("/beets/duplicates", tags=["import"])
@inject
async def beets_duplicates(
        username: str = Depends(require_username),
        rekordbox_xml_controller: RekordboxXMLController = Depends(Provide[Container.rekordbox_xml_controller]),
) -> dict:

    duplicates_removed = await rekordbox_xml_controller.remove_duplicates(username, False)
    return {
        'duplicates_removed': duplicates_removed,
        'success': True,
        'reason': ""
    }

@router.get("/beets/duplicates", tags=["import"])
@inject
async def beets_duplicates(
        public: bool,
        username: str = Depends(require_username),
        rekordbox_xml_controller: RekordboxXMLController = Depends(Provide[Container.rekordbox_xml_controller]),
) -> dict:

    duplicates = await rekordbox_xml_controller.get_duplicates(username, public)
    return {
        'duplicates': duplicates,
        'success': True,
        'reason': ""
    }

@router.post("/beets/import", tags=["import"])
@inject
async def beets_import(
    request: BeetsImportRequest,
    background_tasks: BackgroundTasks,
    user: dict = Depends(require_user),
    beets_client: BeetsClient = Depends(Provide[Container.beets_client]),
    fb_file_handler: FileBrowserFileHandler = Depends(Provide[Container.file_browser_file_handler]),
    rekordbox_xml_controller: RekordboxXMLController = Depends(Provide[Container.rekordbox_xml_controller]),
    db_controller: DbController = Depends(Provide[Container.db_controller]),
    config: Dict = Depends((Provide[Container.config]))
) -> dict:

    if request.public is True:
        public = True
    else:
        public = False
    job_id = ""
    total_n_tracks_for_import = 0
    username = user['username']

    size = fb_file_handler.get_size_of_import(username)
    size_import_bytes = size['size_tracks']
    total_n_tracks_for_import = size['n_tracks']
    exceeded, _1, _2 =  db_controller.user_library_size_exceeded(username, size_import_bytes)
    if exceeded:
        return {
            'success': False,
            'job_id': job_id,
            'n_tracks_for_import': total_n_tracks_for_import,
            'max_library_size_exceeded': True,
            'reason': f"user {username} has exceeded max library size."
        }
    if total_n_tracks_for_import == 0:
        logger.error(
            f"user {username} has attempted to import before uploading any tracks"
        )
        return {
            'success': False,
            'job_id': job_id,
            'max_library_size_exceeded': False,
            'n_tracks_for_import': total_n_tracks_for_import,
            'reason': f"user {username} has attempted to import before uploading any tracks."
        }

    total_n_imported_tracks = await beets_client.get_number_of_tracks(user)
    job_id = db_controller.create_import_job(username, total_n_tracks_for_import, total_n_imported_tracks)
    logger.info(f'importing {total_n_tracks_for_import} tracks for user {username}')

    # Schedule the background task
    background_tasks.add_task(run_import_task, rekordbox_xml_controller, username, public, job_id, db_controller)

    return {
        'success': True,
        'job_id': job_id,
        'max_library_size_exceeded': False,
        'n_tracks_for_import': total_n_tracks_for_import,
        'reason': ""
    }

async def run_import_task(rekordbox_xml_controller, username, public, job_id, db_controller):
    success = True
    try:
        logger.info(f'starting import for user {username}')
        await rekordbox_xml_controller.consume_from_filebrowser(username, public)
    except Exception as ex:
        success = False
        msg = f'error occurred importing the following path in to beets for user {username} {repr(ex)}'
        logger.error(msg, exc_info=True)
    finally:
        logger.info(f'marking import job for user {username} as {success}')
        db_controller.job_completed(job_id, success)


@router.get("/beets/import/progress", tags=["import"])
@inject
async def tracks_imported(
        job_id: str,
        public: bool = False,
        user: dict = Depends(require_user),
        beets_client: BeetsClient = Depends(Provide[Container.beets_client]),
        db_controller: DbController = Depends(Provide[Container.db_controller]),
) -> dict:
    reason = ""
    percentage_complete = 0
    n_tracks_imported = 0
    username = user['username']

    job = db_controller.get_job_by_id(username, job_id)
    original_total_n_imported_tracks: int = job['total_n_imported_tracks']
    original_n_tracks_to_import = job['n_tracks_to_import']
    in_progress = job['in_progress']
    result = job['result']
    if original_n_tracks_to_import:
        total_n_imported_tracks = await beets_client.get_number_of_tracks(user, public)
        n_tracks_imported = total_n_imported_tracks - original_total_n_imported_tracks
        percentage_complete = round((n_tracks_imported / original_n_tracks_to_import) * 100, 2)
        if job['in_progress'] is False and job['result'] is True:
            # it's possible due to duplicate tracks that the maths won't quite work out at 100%.
            # however, if the import job has been marked as complete, then we know we are done.
            percentage_complete = 100
        logger.debug(f'Started with a total of {original_total_n_imported_tracks} already imported tracks.')
        logger.debug(f'A total of {total_n_imported_tracks} have been imported so far.')
        logger.debug(f'have complete {percentage_complete}% out of {original_n_tracks_to_import}')
    else:
        reason = f"no in-progress jobs found for user {username}"
    return {
        'in_progress': in_progress,
        'reason': reason,
        'n_tracks_to_process': original_n_tracks_to_import,
        'n_tracks_processed': n_tracks_imported,
        'percentage_complete': percentage_complete,
        'result': result
    }

@router.get("/beets/import/tracks_imported", tags=["import"])
@inject
async def tracks_imported(
    username: str = Depends(require_username),
) -> dict:
    total_n_imported_tracks = 0
    #total_n_imported_tracks = db_controller.get_number_of_imported_beets_tracks(username)
    logger.info(f'{total_n_imported_tracks} have been imported.')
    return {
        'success': True,
        'reason': "",
        'n_tracks_imported': total_n_imported_tracks
    }

@router.get("/beets/import/tracks_to_be_imported", tags=["import"])
@inject
async def tracks_to_be_imported(
        username: str = Depends(require_username),
        fb_file_handler: FileBrowserFileHandler = Depends(Provide[Container.file_browser_file_handler]),
) -> dict:
    size = fb_file_handler.get_size_of_import(username)
    total_n_tracks = size['n_tracks']
    logger.info(f'{total_n_tracks} waiting to be imported.')
    return {
        'success': True,
        'reason': "",
        'n_tracks_to_be_imported': total_n_tracks
    }
