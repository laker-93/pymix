import logging
from typing import Dict

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, Cookie
from pydantic import BaseModel

from pymix.clients.subsonic_client import SubsonicClient
from pymix.containers import Container
from pymix.controllers.db_controller import DbController
from pymix.handlers.filebrowser_file_handler import FileBrowserFileHandler

router = APIRouter()

logger = logging.getLogger(__name__)

class ClientTrackList(BaseModel):
    tracks: list[dict[str, str]]


@router.post("/match/tracks", tags=["match"])
@inject
async def match_tracks(
        client_tracks: ClientTrackList,
        session_id: str | None = Cookie(None),
        username: str | None = None,
        db_controller: DbController = Depends(Provide[Container.db_controller]),
        subsonic_client: SubsonicClient = Depends(Provide[Container.subsonic_client])
) -> dict:
    success = False
    reason = ""
    user = None
    missing_tracks = []
    matched_tracks = []

    if not username and not session_id:
        success = False
        reason = "must have a username or session id to identify user"
    if username:
        try:
            user = db_controller.get_user(username)
        except Exception as ex:
            logger.error(f'error occurred getting user for {username}', exc_info=True)
            reason = repr(ex)
    if session_id:
        try:
            user = db_controller.get_user_by_session_id(session_id)
        except Exception as ex:
            logger.error(f'error occurred getting user for session id {session_id}', exc_info=True)
            reason = repr(ex)
        else:
            username = user['username']
    if user:
        for client_track in client_tracks.tracks:
            sub_track = await subsonic_client.get_track_match(
                user=user,
                title=client_track['title'],
                artist=client_track['artist'],
            )
            # if have an exact match then can exclude it as the client already having it.
            if sub_track:
                matched_tracks.append(
                    sub_track[0]
                )
            else:
                missing_tracks.append(client_track)
        success = True

    return {
        'success': success,
        'missingTracks': missing_tracks,
        'matchedTracks': matched_tracks,
        'reason': reason
    }
