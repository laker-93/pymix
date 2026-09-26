import logging
from typing import Dict, Optional

import anyio
from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, BackgroundTasks

from pymix.clients.beets_client import BeetsClient
from pymix.containers import Container
from pymix.controllers.db_controller import DbController
from pymix.controllers.serato_controller import SeratoController
from pymix.handlers.filebrowser_file_handler import FileBrowserFileHandler
from pymix.model.serato_export import SeratoExportRequest, SeratoExportResponse
from pymix.model.original_track_meta import UploadAttempt
from pymix.model.serato_import import SeratoImportRequest
from pymix.routers.auth import require_reader, require_uploader
from pymix.services import metrics
from pymix.services.import_progress import failure_reason

router = APIRouter()

logger = logging.getLogger(__name__)


@router.post("/serato/import", tags=["import"])
@inject
async def serato_import(
    background_tasks: BackgroundTasks,
    request: SeratoImportRequest = SeratoImportRequest(),
    user: dict = Depends(require_uploader),
    beets_client: BeetsClient = Depends(Provide[Container.beets_client]),
    fb_file_handler: FileBrowserFileHandler = Depends(Provide[Container.file_browser_file_handler]),
    serato_controller: SeratoController = Depends(Provide[Container.serato_controller]),
    db_controller: DbController = Depends(Provide[Container.db_controller]),
    config: Dict = Depends(Provide[Container.config])
)-> dict:
    job_id = ""
    total_n_tracks_for_import = 0
    username = user['username']

    # Only what this attempt's /sync/map_meta tagged is imported, and so only
    # that is counted and checked against the quota (#38).
    attempt = db_controller.get_upload_attempt(username)
    size = fb_file_handler.get_size_of_import(username, attempt)
    size_import_bytes = size['size_tracks']
    total_n_tracks_for_import = size['n_tracks']
    exceeded, _1, _2 =  db_controller.user_library_size_exceeded(username, size_import_bytes)
    if exceeded:
        metrics.observe_quota_refusal('serato')
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

    total_n_imported_tracks = await beets_client.count_tracks_on_disk(user)
    job_id = db_controller.create_import_job(username, total_n_tracks_for_import, total_n_imported_tracks)
    # The client's manifest, passed through as it arrived: each crate entry's
    # stored path, the subbox_id the client read off that local file, and the cues
    # it read with it. The server never sees those files, so this is the only
    # identity it gets that survives the user moving a track, and the only reading
    # of the cues that reflects what is in Serato now. See pymix.model.serato_import.
    identities = request.track_identities
    n_with_cues = sum(1 for t in identities if t.cues)
    logger.info(
        f'Serato importing {total_n_tracks_for_import} tracks for user {username} '
        f'with {len(identities)} client-resolved track identities '
        f'({n_with_cues} carrying cues)'
    )
    background_tasks.add_task(run_import_task, serato_controller, username, job_id, db_controller,
                              fb_file_handler, total_n_tracks_for_import, user, identities, attempt)
    return {
        'success': True,
        'job_id': job_id,
        'max_library_size_exceeded': False,
        'n_tracks_for_import': total_n_tracks_for_import,
        'reason': ""
    }


async def run_import_task(serato_controller, username, job_id, db_controller, fb_file_handler,
                          total_n_tracks_for_import, user, identities=None,
                          attempt: Optional[UploadAttempt] = None):
    success = True
    reason = ""
    warnings = None
    report = None
    attempt = attempt or UploadAttempt(files={})
    uploaded = []
    selected = None
    try:
        # Everything in uploads/ now, crates included, goes when the job finishes,
        # whatever the outcome (#38). A failed import used to leave its crates for
        # the next one to parse alongside its own.
        uploaded = fb_file_handler.snapshot_uploads(username)
        logger.info(f'starting serato import track staging for user {username}')
        subcrate_path, zip_path, _ = fb_file_handler.get_subcrate_audio_path(username)
        if zip_path:
            # Its contents were never tagged by /sync/map_meta, so staging them
            # would put untagged tracks in the library. No client uploads one.
            logger.warning(f'not importing audio zip {zip_path} for user {username}: its files carry no SUBBOX_ID')
        # reads a tag per file: off the event loop
        selected = await anyio.to_thread.run_sync(fb_file_handler.select_attempt_files, username, attempt)
        report = await serato_controller.create_subsonic_playlists_from_crates(
            user=user,
            serato_crate_path=subcrate_path,
            zip_path=None,
            audio_path=selected.root if selected.files else None,
            audio_files=selected.files,
            identities=identities,
        )
        logger.info(f'finished serato import for user {username}')
    except Exception as ex:
        success = False
        reason = failure_reason(ex)
        msg = f'error occurred importing the following path in to beets for user {username} {repr(ex)}'
        logger.error(msg, exc_info=True)
    else:
        logger.info(
            f'successfully serato imported {total_n_tracks_for_import} for user {username}: '
            f'{report.crates_parsed} crates -> {report.playlists_built} playlists, '
            f'{report.matched} tracks matched, {len(report.skipped)} skipped'
        )
        # A crate track we could not place is not a failure, but it is also not
        # nothing: the user asked for a playlist and got a shorter one. Carry it
        # back so the import screen can say so rather than reporting a clean win.
        warnings = report.warning()
        for skipped in report.skipped:
            logger.info(f'skipped crate entry for {username}: {skipped.crate_path} ({skipped.reason})')
    finally:
        # Before the job is marked complete, so the client's next attempt starts
        # on an empty directory.
        fb_file_handler.finish_upload_attempt(username, uploaded, attempt)
        refused = selected.refused_warning() if selected is not None else ''
        warnings = '; '.join(w for w in (warnings, refused) if w) or None
        logger.info(f'marking serato import job for user {username} as {success}')
        db_controller.job_completed(job_id, success, reason, warnings)



@router.post("/serato/export", tags=["import"])
@inject
async def serato_export(
        request: SeratoExportRequest = SeratoExportRequest(),
        user: dict = Depends(require_reader),
        serato_controller: SeratoController = Depends(Provide[Container.serato_controller]),
) -> SeratoExportResponse:
    """The user's playlists as the crates the *client* will write.

    This used to write `.crate` files on the server against a `user_root` the
    client sent, which made every path in them a prediction about a filesystem
    the server has never seen. Now the server returns what only it knows and the
    client writes the files against the paths it just downloaded to — which is
    also the only way the cues can be written into the user's real audio files.
    See pymix.model.serato_export.
    """
    username = user['username']
    try:
        response = await serato_controller.get_export_structure(
            user=user, playlist_ids=request.playlistIds or None
        )
    except Exception as ex:
        msg = f'error occurred building the serato export for user {username} {repr(ex)}'
        logger.error(msg, exc_info=True)
        return SeratoExportResponse(success=False, reason=msg)
    logger.info(
        f'serato export for user {username}: {response.n_crates} crates, {response.n_tracks} tracks'
    )
    return response
