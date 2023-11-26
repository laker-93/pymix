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

@router.post("/rekordbox/import", tags=["import"])
@inject
async def rekordbox_import(
    session_id: str | None = None,
    username: str | None = None,
    beets_client: BeetsClient = Depends(Provide[Container.beets_client]),
    fb_file_handler: FileBrowserFileHandler = Depends(Provide[Container.file_browser_file_handler]),
    rekordbox_xml_controller: RekordboxXMLController = Depends(Provide[Container.rekordbox_xml_controller]),
    db_controller: DbController = Depends(Provide[Container.db_controller]),
)-> dict:
    success = True
    reason = ""
    beets_output = ""
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
            xml_path, audio_path = fb_file_handler.get_xml_audio_path(username)
            beets_output = await rekordbox_xml_controller.create_subsonic_playlists_from_xml(
                user=user,
                xml_path=xml_path,
                audio_files_to_import=audio_path
            )
        except Exception as ex:
            success = False
            msg = f'error occurred importing the following path in to beets for user {username} {repr(ex)}'
            logger.error(msg, exc_info=True)
            reason = msg
        else:
            total_n_imported_tracks = await beets_client.get_number_of_tracks(user)
        finally:
            db_controller.job_completed(job_id, success)
    return {
        'success': success,
        'imported_tracks': total_n_imported_tracks,
        'beets_output': beets_output,
        'reason': reason
    }

@router.post("/rekordbox/export", tags=["import"])
@inject
async def rekordbox_export(
        session_id: str | None = None,
        user_root: str | None = None,
        username: str | None = None,
        beets_client: BeetsClient = Depends(Provide[Container.beets_client]),
        fb_file_handler: FileBrowserFileHandler = Depends(Provide[Container.file_browser_file_handler]),
        rekordbox_xml_controller: RekordboxXMLController = Depends(Provide[Container.rekordbox_xml_controller]),
        db_controller: DbController = Depends(Provide[Container.db_controller]),
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
        # todo: check number of tracks in xml export matches that in beets matches that in the export zip etc.
        n_beets_tracks = await beets_client.get_number_of_tracks(user)
        #job_id = db_controller.create_import_job(username, total_n_tracks_for_import, total_n_imported_tracks)
        logger.info(f'exporting {n_beets_tracks} tracks for user {username}')
        try:
            xml_path = fb_file_handler.get_xml_output_path(username)
            await rekordbox_xml_controller.create_rekordbox_xml_from_subsonic_playlists(
                user_root,
                user,
                None,
                xml_path
            )
        except Exception as ex:
            success = False
            msg = f'error occurred creating rekordbox xml for user {username} {repr(ex)}'
            logger.error(msg, exc_info=True)
            reason = msg
        else:
            try:
                fb_file_handler.export_subsonic_music(username=username)
            except Exception as ex:
                success = False
                msg = f'error occurred exporting subsonic collection to filebrowser for user {username} {repr(ex)}'
                logger.error(msg, exc_info=True)
                reason = msg
            finally:
                pass
                #todo do the job book keeping
                #db_controller.job_completed(job_id, success)
    return {
        'success': success,
        'n_beets_tracks': n_beets_tracks,
        'beets_output': beets_output,
        'reason': reason
    }

