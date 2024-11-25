import logging
from typing import Dict, Optional

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, BackgroundTasks, Cookie, Depends
from pydantic import BaseModel

from pymix.clients.beets_client import BeetsClient
from pymix.containers import Container
from pymix.controllers.db_controller import DbController
from pymix.controllers.rekordbox_xml_controller import RekordboxXMLController
from pymix.handlers.filebrowser_file_handler import FileBrowserFileHandler

router = APIRouter()

logger = logging.getLogger(__name__)


class BeetsImportRequest(BaseModel):
    public: bool
    username: Optional[str] = None


router = APIRouter()

@router.post("/beets/import", tags=["import"])
@inject
async def beets_import(
    request: BeetsImportRequest,
    background_tasks: BackgroundTasks,
    session_id: str | None = Cookie(None),
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
    username = None
    logger.info(f'got request with session id {session_id} and request username {request.username}')
    if not request.username and not session_id:
        return {
            'success': False,
            'job_id': job_id,
            'n_tracks_for_import': total_n_tracks_for_import,
            'reason': "must have a username or session id to identify user"
        }
    if not request.username and session_id:
        try:
            user = db_controller.get_user_by_session_id(session_id)
        except Exception as ex:
            logger.error(f'error occurred getting user for session id {session_id}', exc_info=True)
            reason = repr(ex)
        else:
            username = user['username']
    elif request.username:
        username = request.username
        user = db_controller.get_user(username)
    if username:
        total_n_tracks_for_import = fb_file_handler.get_number_of_tracks_for_import(username)
        total_n_imported_tracks = await beets_client.get_number_of_tracks(user, public)
        if total_n_tracks_for_import + total_n_imported_tracks > config["max_number_of_tracks"]:
            logger.error(
                f"user {username} has exceeded max number of tracks that can be uploaded of {config['max_number_of_tracks']}."
            )
            return {
                'success': False,
                'job_id': job_id,
                'n_tracks_for_import': total_n_tracks_for_import,
                'reason': f"user {username} has exceeded max number of tracks that can be uploaded."
            }
        if total_n_tracks_for_import == 0:
            logger.error(
                f"user {username} has attempted to import before uploading any tracks"
            )
            return {
                'success': False,
                'job_id': job_id,
                'n_tracks_for_import': total_n_tracks_for_import,
                'reason': f"user {username} has attempted to import before uploading any tracks."
            }

        job_id = db_controller.create_import_job(username, total_n_tracks_for_import, total_n_imported_tracks)
        logger.info(f'importing {total_n_tracks_for_import} tracks for user {username}')

        # Schedule the background task
        background_tasks.add_task(run_import_task, rekordbox_xml_controller, username, public, job_id, db_controller)

    return {
        'success': True,
        'job_id': job_id,
        'n_tracks_for_import': total_n_tracks_for_import,
        'reason': ""
    }

async def run_import_task(rekordbox_xml_controller, username, public, job_id, db_controller):
    success = True
    beets_output = ""
    try:
        logger.info(f'starting import for user {username}')
        beets_output = await rekordbox_xml_controller.consume_from_filebrowser(username, public)
    except Exception as ex:
        success = False
        msg = f'error occurred importing the following path in to beets for user {username} {repr(ex)}'
        logger.error(msg, exc_info=True)
    finally:
        logger.info(f"beets output {beets_output}")
        logger.info(f'marking import job for user {username} as {success}')
        db_controller.job_completed(job_id, success)


@router.get("/beets/import/progress", tags=["import"])
@inject
async def tracks_imported(
        job_id: str,
        public: bool = False,
        username: str | None = None,
        session_id: str | None = Cookie(None),
        beets_client: BeetsClient = Depends(Provide[Container.beets_client]),
        db_controller: DbController = Depends(Provide[Container.db_controller]),
) -> dict:
    in_progress = False
    result = False
    reason = ""
    percentage_complete = 0
    original_n_tracks_to_import = 0
    n_tracks_imported = 0
    if not username and session_id:
        try:
            user = db_controller.get_user_by_session_id(session_id)
        except Exception as ex:
            logger.error(f'error occurred getting user for session id {session_id}', exc_info=True)
            reason = repr(ex)
        else:
            username = user['username']
    elif username:
        user = db_controller.get_user(username)
    if username:
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
    else:
        reason = f"no username found for session id {session_id}"
    return {
        'in_progress': in_progress,
        'reason': reason,
        'n_tracks_to_process': original_n_tracks_to_import,
        'n_tracks_processed': n_tracks_imported,
        'percentage_complete': percentage_complete,
        'result': result
    }



#@router.post("/beets/import", tags=["import"])
#@inject
#async def beets_import(
#    request: BeetsImportRequest,
#    session_id: str | None = Cookie(None),
#    beets_client: BeetsClient = Depends(Provide[Container.beets_client]),
#    fb_file_handler: FileBrowserFileHandler = Depends(Provide[Container.file_browser_file_handler]),
#    rekordbox_xml_controller: RekordboxXMLController = Depends(Provide[Container.rekordbox_xml_controller]),
#    db_controller: DbController = Depends(Provide[Container.db_controller]),
#    config: Dict = Depends((Provide[Container.config]))
#)-> dict:
#
#    if request.public is True:
#        public = True
#    else:
#        public = False
#    success = True
#    reason = ""
#    beets_output = ""
#    total_n_imported_tracks = 0
#    total_n_tracks_for_import = 0
#    username = None
#    logger.info(f'got request with session id {session_id} and request username {request.username}')
#    if not request.username and not session_id:
#        success = False
#        reason = "must have a username or session id to identify user"
#    if not request.username and session_id:
#        try:
#            user = db_controller.get_user_by_session_id(session_id)
#        except Exception as ex:
#            logger.error(f'error occurred getting user for session id {session_id}', exc_info=True)
#            reason = repr(ex)
#        else:
#            username = user['username']
#    elif request.username:
#        username = request.username
#        user = db_controller.get_user(username)
#    if username:
#        total_n_tracks_for_import = fb_file_handler.get_number_of_tracks_for_import(username)
#        total_n_imported_tracks = await beets_client.get_number_of_tracks(user, public)
#        if total_n_tracks_for_import + total_n_imported_tracks > config["max_number_of_tracks"]:
#            logger.error(
#                f"user {username} has exceeded max number of tracks that can be uploaded of {config['max_number_of_tracks']}."
#            )
#            return {
#                'success': False,
#                'imported_tracks': 0,
#                'n_tracks_for_import': total_n_tracks_for_import,
#                'beets_output': "",
#                'reason': f"user {username} has exceeded max number of tracks that can be uploaded."
#            }
#        if total_n_tracks_for_import == 0:
#            logger.error(
#                f"user {username} has attempted to import before uploading any tracks"
#            )
#            return {
#                'success': False,
#                'imported_tracks': 0,
#                'n_tracks_for_import': total_n_tracks_for_import,
#                'beets_output': "",
#                'reason': f"user {username} has attempted to import before uploading any tracks."
#            }
#
#
#
#        job_id = db_controller.create_import_job(username, total_n_tracks_for_import, total_n_imported_tracks)
#        logger.info(f'importing {total_n_tracks_for_import} tracks for user {username}')
#        try:
#            logger.info(f'starting import for user {username}')
#            beets_output = await rekordbox_xml_controller.consume_from_filebrowser(username, public)
#        except Exception as ex:
#            success = False
#            msg = f'error occurred importing the following path in to beets for user {username} {repr(ex)}'
#            logger.error(msg, exc_info=True)
#            reason = msg
#        else:
#            #total_n_tracks = await beets_client.get_number_of_tracks(user)
#            logger.info(f'successfully imported {total_n_tracks_for_import} for user {username}')
#        finally:
#            logger.info(f"beets output {beets_output}")
#            logger.info(f'marking import job for user {username} as {success}')
#            db_controller.job_completed(job_id, success)
#    return {
#        'success': success,
#        'imported_tracks': total_n_imported_tracks,
#        'n_tracks_for_import': total_n_tracks_for_import,
#        'beets_output': beets_output,
#        'reason': reason
#    }


#@router.get("/beets/import/progress", tags=["import"])
#@inject
#async def tracks_imported(
#        public: bool = False,
#        username: str | None = None,
#        session_id: str | None = Cookie(None),
#        beets_client: BeetsClient = Depends(Provide[Container.beets_client]),
#        db_controller: DbController = Depends(Provide[Container.db_controller]),
#) -> dict:
#    import_in_progress = False
#    reason = ""
#    percentage_complete = 0
#    original_n_tracks_to_import = 0
#    n_tracks_imported = 0
#    if not username and session_id:
#        try:
#            user = db_controller.get_user_by_session_id(session_id)
#        except Exception as ex:
#            logger.error(f'error occurred getting user for session id {session_id}', exc_info=True)
#            reason = repr(ex)
#        else:
#            username = user['username']
#    elif username:
#        user = db_controller.get_user(username)
#    if username:
#        n_jobs = db_controller.get_number_of_jobs(username, in_progress=True)
#        logger.info(f'have {n_jobs} in progress jobs for user {username}')
#        # will return 0 on first invocation when the job has yet to be started.
#        if n_jobs > 0:
#            job = db_controller.get_in_progress_job(username)
#            original_total_n_imported_tracks: int = job['total_n_imported_tracks']
#            original_n_tracks_to_import = job['n_tracks_to_import']
#            if original_n_tracks_to_import:
#                total_n_imported_tracks = await beets_client.get_number_of_tracks(user, public)
#                n_tracks_imported = total_n_imported_tracks - original_total_n_imported_tracks
#                percentage_complete = round((n_tracks_imported / original_n_tracks_to_import) * 100, 2)
#                if job['in_progress'] is False and job['result'] is True:
#                    # it's possible due to duplicate tracks that the maths won't quite work out at 100%.
#                    # however, if the import job has been marked as complete, then we know we are done.
#                    percentage_complete = 100
#                logger.debug(f'Started with a total of {original_total_n_imported_tracks} already imported tracks.')
#                logger.debug(f'A total of {total_n_imported_tracks} have been imported so far.')
#                logger.debug(f'have complete {percentage_complete}% out of {original_n_tracks_to_import}')
#                import_in_progress = True
#        else:
#            reason = f"no in-progress jobs found for user {username}"
#    else:
#        reason = f"no username found for session id {session_id}"
#    return {
#        'in_progress': import_in_progress,
#        'reason': reason,
#        'n_tracks_to_process': original_n_tracks_to_import,
#        'n_tracks_processed': n_tracks_imported,
#        'percentage_complete': percentage_complete
#    }

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
