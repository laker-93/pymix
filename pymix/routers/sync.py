import logging
from typing import Dict, Annotated, List, Tuple

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, Query, Cookie
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
    tracks: list[Dict[str, str]]


@router.post("/sync", tags=["sync"])
@inject
async def sync(
        client_tracks: ClientTracks,
        session_id: str | None = None,
        username: str | None = None,
        db_controller: DbController = Depends(Provide[Container.db_controller]),
        fb_file_handler: FileBrowserFileHandler = Depends(Provide[Container.file_browser_file_handler]),
        subsonic_client: SubsonicClient = Depends(Provide[Container.subsonic_client])
) -> dict:
    success = False
    reason = ""
    user = None
    server_tracks_dict = {}
    client_tracks_to_remove = []
    zip_path = None
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
        client_sub_tracks = []
        for client_track in client_tracks.tracks:
            sub_tracks = await subsonic_client.query_tracks_by(
                user=user,
                title=client_track['title'],
                artist=client_track['artist'],
                album=client_track['album'],
            )
            # if have an exact match then can exclude it as the client already having it.
            if len(sub_tracks) == 1:
                client_sub_tracks.append(
                    sub_tracks[0]
                )
            else:
                logger.info(f'no track found matching {client_track}. Adding track to tracks to be deleted on client.')
                client_tracks_to_remove.append(client_track)

        async for server_tracks in subsonic_client.get_all_tracks(user, 200):
            for server_track in server_tracks:
                logger.debug(f'adding server track {server_track}')
                server_tracks_dict[server_track.sub_track_id] = server_track

        client_tracks_to_remove = []
        for client_track in client_sub_tracks:
            if client_track.sub_track_id not in server_tracks_dict:
                client_tracks_to_remove.append(client_track)
            else:
                server_tracks_dict.pop(client_track.sub_track_id)

        n_tracks_zipped, zip_path = fb_file_handler.sync(
            username=username,
            tracks_to_zip=list(server_tracks_dict.values())
        )
        success = True

    return {
        'success': success,
        'filesNotOnServer': client_tracks_to_remove,
        'nTracksExported': len(server_tracks_dict),
        'zipPath': zip_path,
        'reason': reason
    }



class SyncPlaylistArgs(BaseModel):
    ids: list[str]
    tracks: list[Dict[str, str]] = None


@router.post("/sync/playlists", tags=["sync"])
@inject
async def sync(
        args: SyncPlaylistArgs,
        session_id: str | None = Cookie(None),
        username: str | None = None,
        db_controller: DbController = Depends(Provide[Container.db_controller]),
        fb_file_handler: FileBrowserFileHandler = Depends(Provide[Container.file_browser_file_handler]),
        subsonic_client: SubsonicClient = Depends(Provide[Container.subsonic_client])
) -> dict:
    success = False
    reason = ""
    user = None
    server_tracks_dict = {}
    zip_path = None
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
        all_tracks = []
        for playlist_id in args.ids:
            tracks = await subsonic_client.get_playlist_tracks(user, playlist_id)
            all_tracks.extend(tracks)

        n_tracks_zipped, zip_path = fb_file_handler.sync(
            username=username,
            tracks_to_zip=all_tracks
        )
        success = True

    return {
        'success': success,
        'nTracksExported': len(server_tracks_dict),
        'zipPath': zip_path,
        'reason': reason
    }

