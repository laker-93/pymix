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
from pymix.routers.auth import require_uploader, require_username
from pymix.services.automatch_service import AutomatchService
from pymix.services.import_progress import (
    failure_reason,
    ImportProgressReporter,
    overall_percentage,
    UNRECORDED_FAILURE_REASON,
)

router = APIRouter()

logger = logging.getLogger(__name__)


class BeetsImportRequest(BaseModel):
    public: bool


class BeetsReimportRequest(BaseModel):
    # Raw beets query syntax (e.g. "path:Artist/Album", "album:'Vol. 1'"),
    # scoped to whatever subset of the caller's own library they're fixing --
    # see AutomatchService.manual_reimport.
    query: str


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
    user: dict = Depends(require_uploader),
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

    total_n_imported_tracks = await beets_client.count_tracks_on_disk(user)
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
    reason = ""
    try:
        logger.info(f'starting import for user {username}')
        await rekordbox_xml_controller.consume_from_filebrowser(
            username, public, progress=ImportProgressReporter(db_controller, job_id)
        )
    except Exception as ex:
        success = False
        reason = failure_reason(ex)
        msg = f'error occurred importing the following path in to beets for user {username} {repr(ex)}'
        logger.error(msg, exc_info=True)
    finally:
        logger.info(f'marking import job for user {username} as {success}')
        db_controller.job_completed(job_id, success, reason)


@router.post("/beets/reimport", tags=["import"])
@inject
async def beets_reimport(
    request: BeetsReimportRequest,
    user: dict = Depends(require_uploader),
    automatch_service: AutomatchService = Depends(Provide[Container.automatch_service]),
) -> dict:
    """Reimport the caller's own library, scoped to ``request.query`` (raw beets
    query syntax -- e.g. ``path:Artist/Album`` for one busted subdirectory), against
    MusicBrainz. Synchronous: the query is expected to be small and deliberate
    (laker-93/pymix#95's manual escape hatch), not a whole-library sweep -- there is
    no job/progress polling here, unlike /beets/import.
    """
    username = user['username']
    result = await automatch_service.manual_reimport(user, request.query)

    if result.errored:
        logger.error(f"beets reimport failed for {username}, query {request.query!r}")
        return {
            'success': False,
            'matched': result.matched,
            'nomatch': result.nomatch,
            'reason': f"reimport failed for query {request.query!r} -- see server logs.",
        }

    n_matched, n_nomatch = len(result.matched), len(result.nomatch)
    reason = "" if (n_matched or n_nomatch) else f"query {request.query!r} matched no tracks in {username}'s library"
    logger.info(f"beets reimport for {username}, query {request.query!r}: {n_matched} matched, {n_nomatch} nomatch")
    return {
        'success': True,
        'matched': result.matched,
        'nomatch': result.nomatch,
        'reason': reason,
    }


@router.get("/beets/import/progress", tags=["import"])
@inject
# Named `import_progress`, not `tracks_imported`: /beets/import/tracks_imported below
# defines a second function of that name, and the later definition shadows this one at
# module scope — so the route worked but nothing could import or test it by name.
async def import_progress(
        job_id: str,
        public: bool = False,
        user: dict = Depends(require_uploader),
        beets_client: BeetsClient = Depends(Provide[Container.beets_client]),
        db_controller: DbController = Depends(Provide[Container.db_controller]),
) -> dict:
    """
    Progress of an import job, per phase.

    An import is several passes, not one — `beet import`, then the subbox_id map,
    then the XML metadata — and only the first is visible in beets' track count.
    Reporting that count alone pinned the percentage at 100 for the whole tail
    (laker-93/pymix#51), so the response also carries the phase the job is in and
    that phase's own n/total, and `percentage_complete` composes the two. Only a
    finished job ever reads 100.

    A job with no audio to import is a real job, not an absent one: a metadata-only
    Rekordbox re-import (every track already uploaded) still runs the two tail
    passes, and the client now polls it to the end rather than declaring success
    the moment the upload returns (subbox-app#55). Its progress therefore comes
    from the phase columns alone — the audio pass has nothing to do and counts as
    already done.
    """
    reason = ""
    percentage_complete = 0
    n_tracks_imported = 0
    username = user['username']

    job = db_controller.get_job_by_id(username, job_id)
    original_total_n_imported_tracks: int = job['total_n_imported_tracks']
    original_n_tracks_to_import = job['n_tracks_to_import']
    in_progress = job['in_progress']
    result = job['result']
    phase = job.get('phase')
    phase_n_processed = job.get('phase_n_processed') or 0
    phase_n_total = job.get('phase_n_total') or 0
    phase_fraction = (phase_n_processed / phase_n_total) if phase_n_total else 0.0
    if original_n_tracks_to_import:
        # Reads the landed-file count off the host filesystem rather than shelling
        # `beet stats` into the container on every poll (laker-93/pymix#106).
        total_n_imported_tracks = await beets_client.count_tracks_on_disk(user, public)
        n_tracks_imported = total_n_imported_tracks - original_total_n_imported_tracks
        audio_fraction = n_tracks_imported / original_n_tracks_to_import
        logger.debug(f'Started with a total of {original_total_n_imported_tracks} already imported tracks.')
        logger.debug(f'A total of {total_n_imported_tracks} have been imported so far.')
    else:
        # Nothing to land, so the audio pass is vacuously complete — otherwise a
        # metadata-only job would sit at 0% for its whole run.
        audio_fraction = 1.0
    percentage_complete = overall_percentage(phase, phase_fraction, audio_fraction)
    if in_progress is False and result is True:
        # it's possible due to duplicate tracks that the maths won't quite work out at 100%.
        # however, if the import job has been marked as complete, then we know we are done.
        percentage_complete = 100
    if in_progress is False and result is False:
        # The one thing the user's failure screen has to go on. A failed job also
        # keeps the phase it died in, so the client can tell "the audio never
        # imported" from "the tracks are in, the metadata pass broke".
        reason = job.get('reason') or UNRECORDED_FAILURE_REASON
    # A job that succeeded can still have left work undone — a Serato import whose
    # crates named tracks the user has never uploaded, say. That belongs in front
    # of the user, but as a notice rather than a failure, so it travels separately
    # from `reason` (migration 018).
    warnings = job.get('warnings') if in_progress is False else None
    logger.debug(f'in phase {phase} ({phase_n_processed}/{phase_n_total})')
    logger.debug(f'have complete {percentage_complete}% out of {original_n_tracks_to_import}')
    return {
        'in_progress': in_progress,
        'reason': reason,
        'n_tracks_to_process': original_n_tracks_to_import,
        'n_tracks_processed': n_tracks_imported,
        'percentage_complete': percentage_complete,
        'phase': phase,
        'phase_n_processed': phase_n_processed,
        'phase_n_total': phase_n_total,
        'result': result,
        'warnings': warnings
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
