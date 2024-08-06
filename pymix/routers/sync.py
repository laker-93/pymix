import logging
from typing import Dict

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends
from anyio import to_process
from pydantic import BaseModel

from pymix.clients.beets_client import BeetsClient
from pymix.clients.subsonic_client import SubsonicClient
from pymix.containers import Container
from pymix.controllers.db_controller import DbController
from pymix.controllers.rekordbox_xml_controller import RekordboxXMLController
from pymix.handlers.filebrowser_file_handler import FileBrowserFileHandler

router = APIRouter()

logger = logging.getLogger(__name__)

class ClientTracks(BaseModel):
    sub_id: int



@router.post("/sync", tags=["sync"])
@inject
async def sync(
        client_tracks: Annotated[List[Tuple[str, str]], Query()],
        session_id: str | None = None,
        username: str | None = None,
        db_controller: DbController = Depends(Provide[Container.db_controller]),
        fb_file_handler: FileBrowserFileHandler = Depends(Provide[Container.file_browser_file_handler]),
        subsonic_client: SubsonicClient = Depends(Provide[Container.subsonic_client])
)-> dict:
    success = True
    reason = ""
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
        # todo - get the subsonic id for the client tracks passed in.
        client_sub_tracks = []
        client_tracks_to_remove = []
        for client_track in client_tracks:
            sub_track = await subsonic_client.query_tracks_by_title_and_artist(
                user=user,
                title=client_track[0],
                artist=client_track[1]
            )
            if sub_track:
                client_sub_tracks.append(
                    sub_track
                )
            else:
                logger.info(f'no track found matching {client_track}. Adding track to tracks to be deleted on client.')
                client_tracks_to_remove.append(client_track)

        server_tracks_dict = {}
        async for server_tracks in subsonic_client.get_all_tracks():
            for server_track in server_tracks:
                server_tracks_dict[server_track.sub_track_id] = server_track
        await fb_file_handler.sync(
            username=username,
            client_tracks=client_sub_tracks,
            server_tracks=server_tracks_dict
        )

    return {
        'success': success,
        'filesNotOnServer': client_tracks_to_remove,
        'nTracksExported': n_tracks_exported,
        'zipPath': zip_path,
        'beets_output': beets_output,
        'reason': reason
    }

