import logging
from typing import Dict

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends
from anyio import to_process

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
    config: Dict = Depends(Provide[Container.config]),
)-> dict:
    success = True
    reason = ""
    beets_output = ""
    total_n_imported_tracks = 0
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
        if user['dj'] is False:
            logger.error(
                f"user {username} has attempted to import but did not create a DJ account."
            )
            return {
                'success': False,
                'imported_tracks': 0,
                'n_tracks_for_import': total_n_tracks_for_import,
                'beets_output': "",
                'reason': f"user {username} has attempted to import but does not have a DJ account."
            }
        total_n_tracks_for_import = fb_file_handler.get_number_of_tracks_for_import(username)
        total_n_imported_tracks = await beets_client.get_number_of_tracks(user)
        if total_n_tracks_for_import + total_n_imported_tracks > config["max_number_of_tracks"]:
            logger.error(
                f"user {username} has exceeded max number of tracks that can be uploaded of {config['max_number_of_tracks']}."
            )
            return {
                'success': False,
                'imported_tracks': 0,
                'n_tracks_for_import': total_n_tracks_for_import,
                'beets_output': "",
                'reason': f"user {username} has exceeded max number of tracks that can be uploaded."
            }
        if total_n_tracks_for_import == 0:
            logger.error(
                f"user {username} has attempted to import before uploading any tracks"
            )
            return {
                'success': False,
                'imported_tracks': 0,
                'n_tracks_for_import': total_n_tracks_for_import,
                'beets_output': "",
                'reason': f"user {username} has attempted to import before uploading any tracks."
            }


        job_id = db_controller.create_import_job(username, total_n_tracks_for_import, total_n_imported_tracks)
        logger.info(f'RB importing {total_n_tracks_for_import} tracks for user {username}')
        try:
            logger.info(f'starting RB import track staging for user {username}')
            xml_path, audio_path = fb_file_handler.get_xml_audio_path(username)
            logger.info(f'finished RB import track staging for user {username}')
            beets_output = await rekordbox_xml_controller.create_subsonic_playlists_from_xml(
                user=user,
                xml_path=xml_path,
                audio_files_to_import=audio_path
            )
            logger.info(f'finished RB import for user {username}')
        except Exception as ex:
            success = False
            msg = f'error occurred importing the following path in to beets for user {username} {repr(ex)}'
            logger.error(msg, exc_info=True)
            reason = msg
        else:
            total_n_imported_tracks = await beets_client.get_number_of_tracks(user)
            logger.info(f'successfully RB imported {total_n_tracks_for_import} for user {username}')
        finally:
            logger.info(f'marking RB import job for user {username} as {success}')
            db_controller.job_completed(job_id, success)
    return {
        'success': success,
        'imported_tracks': total_n_imported_tracks,
        'n_tracks_for_import': total_n_tracks_for_import,
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
        if user['dj'] is False:
            logger.error(
                f"user {username} has attempted to import but did not create a DJ account."
            )
            return {
                'success': False,
                'n_beets_tracks': n_beets_tracks,
                'beets_output': "",
                'reason': f"user {username} has attempted to import but does not have a DJ account."
            }
        # todo: check number of tracks in xml export matches that in beets matches that in the export zip etc.
        n_beets_tracks = await beets_client.get_number_of_tracks(user)
        job_id = db_controller.create_export_job(username, n_beets_tracks)
        logger.info(f'exporting {n_beets_tracks} tracks for user {username}')
        try:
            xml_output_path = fb_file_handler.get_xml_output_path(username)
            await rekordbox_xml_controller.create_rekordbox_xml_from_subsonic_playlists(
                user_root=user_root,
                user=user,
                xml_path=None,
                xml_output_path=xml_output_path
            )
        except Exception as ex:
            success = False
            msg = f'error occurred creating rekordbox xml for user {username} {repr(ex)}'
            logger.error(msg, exc_info=True)
            reason = msg
        else:
            try:
                logger.info(f'starting to prepare subbox export zip of {n_beets_tracks} tracks for user {user}')
                n_tracks_zipped = await to_process.run_sync(fb_file_handler.export_subsonic_music, config["db"]["path"], config["app_env"], username, job_id)
            except Exception as ex:
                success = False
                msg = f'error occurred exporting subsonic collection to filebrowser for user {username} {repr(ex)}'
                logger.error(msg, exc_info=True)
                reason = msg
            finally:
                logger.info(f'zipped {n_tracks_zipped} tracks. Marking success of rekordbox export job for user {username} as {success}')
                db_controller.job_completed(job_id, success)
    return {
        'success': success,
        'n_beets_tracks': n_beets_tracks,
        'beets_output': beets_output,
        'reason': reason
    }

