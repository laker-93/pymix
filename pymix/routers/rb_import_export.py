import logging
from typing import Dict, Optional

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, Cookie, BackgroundTasks
from anyio import to_process
from pydantic import BaseModel

from pymix.clients.beets_client import BeetsClient
from pymix.containers import Container
from pymix.controllers.db_controller import DbController
from pymix.controllers.rekordbox_xml_controller import RekordboxXMLController
from pymix.handlers.filebrowser_file_handler import FileBrowserFileHandler
from pymix.routers.beets_import import BeetsImportRequest

router = APIRouter()

logger = logging.getLogger(__name__)

class RBImportRequest(BaseModel):
    playlistNames: list[list[str]]



@router.post("/rekordbox/import", tags=["import"])
@inject
async def rekordbox_import(
    request: RBImportRequest,
    background_tasks: BackgroundTasks,
    session_id: str | None = Cookie(None),
    beets_client: BeetsClient = Depends(Provide[Container.beets_client]),
    fb_file_handler: FileBrowserFileHandler = Depends(Provide[Container.file_browser_file_handler]),
    rekordbox_xml_controller: RekordboxXMLController = Depends(Provide[Container.rekordbox_xml_controller]),
    db_controller: DbController = Depends(Provide[Container.db_controller]),
    config: Dict = Depends(Provide[Container.config]),
)-> dict:
    job_id = ""
    username = None
    success = False
    user = None
    reason = ""
    total_n_tracks_for_import = 0
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

    if username:
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
            logger.info(
                f"user {username} has attempted to import before uploading any tracks"
            )
            # this path is ok e.g. for meta changes only
            #return {
            #    'success': False,
            #    'job_id': job_id,
            #    'max_library_size_exceeded': False,
            #    'n_tracks_for_import': total_n_tracks_for_import,
            #    'reason': f"user {username} has not uploaded any files to import."
            #}

        total_n_imported_tracks = await beets_client.get_number_of_tracks(user)
        job_id = db_controller.create_import_job(username, total_n_tracks_for_import, total_n_imported_tracks)
        logger.info(f'RB importing {total_n_tracks_for_import} tracks for user {username}')
        requested_playlists = [p for p in request.playlistNames if p]
        background_tasks.add_task(run_import_task, rekordbox_xml_controller, username, job_id, db_controller,
                      fb_file_handler, total_n_tracks_for_import, user, requested_playlists)
        success = True
        reason = ""

    return {
        'success': success,
        'job_id': job_id,
        'max_library_size_exceeded': False,
        'n_tracks_for_import': total_n_tracks_for_import,
        'reason': reason
    }


async def run_import_task(rekordbox_xml_controller, username, job_id, db_controller, fb_file_handler,
                          total_n_tracks_for_import, user, playlist_names: list[list[str]]):
    success = True
    beets_output = ""
    try:
        xml_path, zip_path, audio_path = fb_file_handler.get_xml_data_path(username)
        logger.info(f'starting RB import track staging for user {username} on {xml_path} and {audio_path}')
        logger.info(f'finished RB import track staging for user {username}')
        beets_output = await rekordbox_xml_controller.create_subsonic_playlists_from_xml(
            user=user,
            xml_path=xml_path,
            zip_path=zip_path,
            audio_path=audio_path,
            playlist_names=playlist_names,
        )
        logger.info(f'finished RB import for user {username}')
    except Exception as ex:
        success = False
        msg = f'error occurred importing the following path in to beets for user {username} {repr(ex)}'
        logger.error(msg, exc_info=True)
    else:
        #total_n_imported_tracks = await beets_client.get_number_of_tracks(user)
        logger.info(f'successfully RB imported {total_n_tracks_for_import} for user {username}')
    finally:
        logger.info(f"beets output {beets_output}")
        logger.info(f'marking RB import job for user {username} as {success}')
        db_controller.job_completed(job_id, success)


class RBExportRequest(BaseModel):
    user_root: str
    playlistIds: list[str] = []

@router.post("/rekordbox/export", tags=["import"])
@inject
async def rekordbox_export(
        request: RBExportRequest,
        session_id: str | None = Cookie(None),
        username: str | None = None,
        beets_client: BeetsClient = Depends(Provide[Container.beets_client]),
        fb_file_handler: FileBrowserFileHandler = Depends(Provide[Container.file_browser_file_handler]),
        rekordbox_xml_controller: RekordboxXMLController = Depends(Provide[Container.rekordbox_xml_controller]),
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
            xml_output_path = fb_file_handler.get_xml_output_path(username)
            requested_playlist_ids = [p for p in request.playlistIds if p]
            await rekordbox_xml_controller.create_rekordbox_xml_from_subsonic_playlists(
                user_root=user_root,
                user=user,
                xml_path=None,
                xml_output_path=xml_output_path,
                playlist_ids=requested_playlist_ids or None,
            )
        except Exception as ex:
            success = False
            msg = f'error occurred creating rekordbox xml for user {username} {repr(ex)}'
            logger.error(msg, exc_info=True)
            reason = msg
        #else:
            # try:
            #     logger.info(f'starting to prepare subbox export zip of {n_beets_tracks} tracks for user {user}')
            #     n_tracks_zipped = await to_process.run_sync(fb_file_handler.export_subsonic_music, config["db"]["path"], config["app_env"], username, job_id)
            # except Exception as ex:
            #     success = False
            #     msg = f'error occurred exporting subsonic collection to filebrowser for user {username} {repr(ex)}'
            #     logger.error(msg, exc_info=True)
            #     reason = msg
            # finally:
            #     logger.info(f'zipped {n_tracks_zipped} tracks. Marking success of rekordbox export job for user {username} as {success}')
            #     db_controller.job_completed(job_id, success)
    return {
        'success': success,
        'n_beets_tracks': n_beets_tracks,
        'beets_output': beets_output,
        'reason': reason
    }

