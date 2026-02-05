import logging
from typing import Dict

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, Cookie, BackgroundTasks

from pymix.clients.beets_client import BeetsClient
from pymix.containers import Container
from pymix.controllers.db_controller import DbController
from pymix.controllers.serato_controller import SeratoController
from pymix.handlers.filebrowser_file_handler import FileBrowserFileHandler
from pymix.routers.rb_import_export import RBExportRequest

router = APIRouter()

logger = logging.getLogger(__name__)


@router.post("/serato/import", tags=["import"])
@inject
async def serato_import(
    background_tasks: BackgroundTasks,
    session_id: str | None = Cookie(None),
    username: str | None = None,
    beets_client: BeetsClient = Depends(Provide[Container.beets_client]),
    fb_file_handler: FileBrowserFileHandler = Depends(Provide[Container.file_browser_file_handler]),
    serato_controller: SeratoController = Depends(Provide[Container.serato_controller]),
    db_controller: DbController = Depends(Provide[Container.db_controller]),
    config: Dict = Depends(Provide[Container.config])
)-> dict:
    success = True
    job_id = ""
    reason = ""
    user = None
    total_n_tracks_for_import = 0
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
        size = fb_file_handler.get_size_of_import(username)
        size_import_bytes = size['size_tracks']
        total_n_tracks_for_import = size['n_tracks']
        if db_controller.user_library_size_exceeded(username, size_import_bytes):
            return {
                'success': False,
                'imported_tracks': 0,
                'n_tracks_for_import': total_n_tracks_for_import,
                'beets_output': "",
                'reason': f"user {username} has exceeded max library size."
            }
        if total_n_tracks_for_import == 0:
            logger.error(
                f"user {username} has attempted to import before uploading any tracks"
            )
            # this path is ok for meta changes only
            #return {
            #    'success': False,
            #    'imported_tracks': 0,
            #    'n_tracks_for_import': total_n_tracks_for_import,
            #    'beets_output': "",
            #    'reason': f"user {username} has attempted to import before uploading any tracks."
            #}

        total_n_imported_tracks = await beets_client.get_number_of_tracks(user)
        job_id = db_controller.create_import_job(username, total_n_tracks_for_import, total_n_imported_tracks)
        logger.info(f'Serato importing {total_n_tracks_for_import} tracks for user {username}')
        background_tasks.add_task(run_import_task, serato_controller, username, job_id, db_controller,
                                  fb_file_handler, total_n_tracks_for_import, user)
        success = True
        reason = ""
    return {
        'success': success,
        'job_id': job_id,
        'max_library_size_exceeded': False,
        'n_tracks_for_import': total_n_tracks_for_import,
        'reason': reason
    }


async def run_import_task(serato_controller, username, job_id, db_controller, fb_file_handler,
                          total_n_tracks_for_import, user):
    success = True
    beets_output = ""
    try:
        logger.info(f'starting serato import track staging for user {username}')
        subcrate_path, zip_path, audio_path = fb_file_handler.get_subcrate_audio_path(username)
        beets_output = await serato_controller.create_subsonic_playlists_from_crates(
            user=user,
            serato_crate_path=subcrate_path,
            zip_path=zip_path,
            audio_path=audio_path
        )
        logger.info(f'finished serato import for user {username}')
    except Exception as ex:
        success = False
        msg = f'error occurred importing the following path in to beets for user {username} {repr(ex)}'
        logger.error(msg, exc_info=True)
    else:
        logger.info(f'successfully serato imported {total_n_tracks_for_import} for user {username}')
    finally:
        logger.info(f"beets output {beets_output}")
        logger.info(f'marking serato import job for user {username} as {success}')
        db_controller.job_completed(job_id, success)



@router.post("/serato/export", tags=["import"])
@inject
async def serato_export(
        request: RBExportRequest,
        session_id: str | None = Cookie(None),
        username: str | None = None,
        beets_client: BeetsClient = Depends(Provide[Container.beets_client]),
        fb_file_handler: FileBrowserFileHandler = Depends(Provide[Container.file_browser_file_handler]),
        serato_controller: SeratoController = Depends(Provide[Container.serato_controller]),
        db_controller: DbController = Depends(Provide[Container.db_controller]),
        config: Dict = Depends(Provide[Container.config])
)-> dict:
    success = True
    reason = ""
    beets_output = ""
    n_beets_tracks = 0
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
        user_root = request.user_root
        # todo: check number of tracks in xml export matches that in beets matches that in the export zip etc.
        n_beets_tracks = await beets_client.get_number_of_tracks(user)
        #job_id = db_controller.create_export_job(username, n_beets_tracks)
        logger.info(f'exporting {n_beets_tracks} tracks for user {username}')
        try:
            output_path = fb_file_handler.get_crate_output_path(username)
            await serato_controller.create_crates_from_subsonic_playlists(
                user_root=user_root,
                user=user,
                output_path=output_path
            )
        except Exception as ex:
            success = False
            msg = f'error occurred creating serato crates for user {username} {repr(ex)}'
            logger.error(msg, exc_info=True)
            reason = msg
        #else:
        #    try:
        #        logger.info(f'starting to prepare subbox export zip of {n_beets_tracks} tracks for user {user}')
        #        n_tracks_zipped = await to_process.run_sync(fb_file_handler.export_subsonic_music, config["db"]["path"], config["app_env"], username, job_id)
        #    except Exception as ex:
        #        success = False
        #        msg = f'error occurred exporting subsonic collection to filebrowser for user {username} {repr(ex)}'
        #        logger.error(msg, exc_info=True)
        #        reason = msg
        #    finally:
        #        logger.info(f'zipped {n_tracks_zipped} tracks. marking serato export job for user {username} as {success}')
        #        db_controller.job_completed(job_id, success)
    return {
        'success': success,
        'n_beets_tracks': n_beets_tracks,
        'beets_output': beets_output,
        'reason': reason
    }

