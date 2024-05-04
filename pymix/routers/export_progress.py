import logging
import zipfile

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends

from pymix.clients.beets_client import BeetsClient
from pymix.containers import Container
from pymix.controllers.db_controller import DbController
from pymix.controllers.rekordbox_xml_controller import RekordboxXMLController
from pymix.handlers.filebrowser_file_handler import FileBrowserFileHandler

router = APIRouter()

logger = logging.getLogger(__name__)

@router.get("/export/progress", tags=["import"])
@inject
async def export_progress(
        session_id: str | None = None,
        username: str | None = None,
        db_controller: DbController = Depends(Provide[Container.db_controller]),
) -> dict:
    export_in_progress = False
    reason = ""
    percentage_complete = 0
    total_n_tracks_to_export = 0
    n_exported_tracks = 0
    if not username and session_id:
        try:
            user = db_controller.get_user_by_session_id(session_id)
        except Exception as ex:
            logger.error(f'error occurred getting user for session id {session_id}', exc_info=True)
            reason = repr(ex)
        else:
            username = user['username']
    if username:
        n_jobs = db_controller.get_number_of_jobs(username, in_progress=True)
        # will return 0 on first invocation when the job has yet to be started.
        if n_jobs > 0:
            job = db_controller.get_in_progress_job(username)
            total_n_tracks_to_export: int = job['total_n_tracks_to_export']
            n_exported_tracks: int = job['n_exported_tracks']
            logger.info(f'lajp n exported tracks {n_exported_tracks}')
            if n_exported_tracks:
                percentage_complete = round((n_exported_tracks / total_n_tracks_to_export) * 100, 2)
                if job['in_progress'] is False and job['result'] is True:
                    # it's possible due to duplicate tracks that the maths won't quite work out at 100%.
                    # however, if the import job has been marked as complete, then we know we are done.
                    percentage_complete = 100
                logger.debug(f'A total of {n_exported_tracks} have been exported out of {total_n_tracks_to_export} so far.')
                logger.debug(f'have complete {percentage_complete}% out of {total_n_tracks_to_export}')
                export_in_progress = True
        else:
            reason = f"no in-progress jobs found for user {username}"
    else:
        reason = f"no username found for session id {session_id}"
    return {
        'in_progress': export_in_progress,
        'reason': reason,
        'n_tracks_to_process': total_n_tracks_to_export,
        'n_tracks_processed': n_exported_tracks,
        'percentage_complete': percentage_complete
    }
