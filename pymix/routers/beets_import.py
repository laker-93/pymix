import logging

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends

from pymix.clients.beets_client import BeetsClient
from pymix.containers import Container
from pymix.controllers.db_controller import DbController
from pymix.controllers.rekordbox_xml_controller import RekordboxXMLController
from pymix.handlers.filebrowser_file_handler import FileBrowserFileHandler

router = APIRouter()

logger = logging.getLogger(__name__)

@router.post("/beets/import", tags=["import"])
@inject
async def beets_import(
    session_id: str | None = None,
    username: str | None = None,
    beets_client: BeetsClient = Depends(Provide[Container.beets_client]),
    fb_file_handler: FileBrowserFileHandler = Depends(Provide[Container.file_browser_file_handler]),
    rekordbox_xml_controller: RekordboxXMLController = Depends(Provide[Container.rekordbox_xml_controller]),
    db_controller: DbController = Depends(Provide[Container.db_controller]),
)-> dict:
    success = True
    reason = ""
    total_n_imported_tracks = 0
    if not username and not session_id:
        success = False
        reason = "must have a username or session id to identify user"
    if not username and session_id:
        try:
            user = db_controller.get_user_by_session_id(session_id)
        except Exception as ex:
            logger.error(f'error occurred getting user for session id {session_id}', exc_info=True)
            reason = repr(ex)
        else:
            username = user['username']
    if username:
        total_n_tracks_for_import = fb_file_handler.get_number_of_tracks_for_import(username)
        total_n_imported_tracks = await beets_client.get_number_of_tracks(user)
        job_id = db_controller.create_import_job(username, total_n_tracks_for_import, total_n_imported_tracks)
        logger.info(f'importing {total_n_tracks_for_import} tracks for user {username}')
        try:
            await rekordbox_xml_controller.consume_from_filebrowser(username)
        except Exception as ex:
            success = False
            msg = f'error occurred importing the following path in to beets for user {username} {repr(ex)}'
            logger.error(msg, exc_info=True)
            reason = msg
        else:
            total_n_imported_tracks = await beets_client.get_number_of_tracks(user)
        db_controller.job_completed(job_id, success)
    return {
        'success': success,
        'imported_tracks': total_n_imported_tracks,
        'reason': reason
    }


@router.get("/beets/import/progress", tags=["import"])
@inject
async def tracks_imported(
        session_id: str | None = None,
        username: str | None = None,
        beets_client: BeetsClient = Depends(Provide[Container.beets_client]),
        db_controller: DbController = Depends(Provide[Container.db_controller]),
) -> dict:
    success = False
    reason = ""
    percentage_complete = 0
    if not username and session_id:
        try:
            user = db_controller.get_user_by_session_id(session_id)
        except Exception as ex:
            logger.error(f'error occurred getting user for session id {session_id}', exc_info=True)
            reason = repr(ex)
        else:
            username = user['username']
    if username:
        # this will raise an exception on first invocation when the job has yet to be started.
        n_jobs = db_controller.get_number_of_jobs(username)
        if n_jobs > 0:
            job = db_controller.get_import_job(username)
            original_total_n_imported_tracks: int = job['total_n_imported_tracks']
            original_n_tracks_to_import = job['n_tracks_to_import']
            total_n_imported_tracks = await beets_client.get_number_of_tracks(user)
            imported_diff = total_n_imported_tracks - original_total_n_imported_tracks
            percentage_complete = imported_diff / original_n_tracks_to_import
            logger.info(f'A total of {total_n_imported_tracks} have been imported.')
            logger.info(f'have complete {percentage_complete}% out of {original_n_tracks_to_import}')
            success = True
    return {
        'success': success,
        'reason': reason,
        'percentage_complete': percentage_complete
    }

@router.get("/beets/import/tracks_imported", tags=["import"])
@inject
async def tracks_imported(
    session_id: str | None = None,
    username: str | None = None,
    db_controller: DbController = Depends(Provide[Container.db_controller]),
) -> dict:
    success = False
    reason = ""
    total_n_imported_tracks = 0
    if not username and session_id:
        try:
            user = db_controller.get_user_by_session_id(session_id)
        except Exception as ex:
            logger.error(f'error occurred getting user for session id {session_id}', exc_info=True)
            reason = repr(ex)
        else:
            username = user['username']
    if username:
        total_n_imported_tracks = docker_controller.get_number_of_imported_beets_tracks(username)
        logger.info(f'{total_n_imported_tracks} have been imported.')
        success = True
    return {
        'success': success,
        'reason': reason,
        'n_tracks_imported': total_n_imported_tracks
    }

@router.get("/beets/import/tracks_to_be_imported", tags=["import"])
@inject
async def tracks_to_be_imported(
        session_id: str | None = None,
        username: str | None = None,
        fb_file_handler: FileBrowserFileHandler = Depends(Provide[Container.file_browser_file_handler]),
        db_controller: DbController = Depends(Provide[Container.db_controller]),
) -> dict:
    success = False
    reason = ""
    total_n_tracks = 0
    if not username and session_id:
        try:
            user = db_controller.get_user_by_session_id(session_id)
        except Exception as ex:
            logger.error(f'error occurred getting user for session id {session_id}', exc_info=True)
            reason = repr(ex)
        else:
            username = user['username']
    if username:
        total_n_tracks = fb_file_handler.get_number_of_tracks_for_import(username)
        logger.info(f'{total_n_tracks} waiting to be imported.')
        success = True
    return {
        'success': success,
        'reason': reason,
        'n_tracks_to_be_imported': total_n_tracks
    }
