import logging
from typing import Dict, Annotated, List, Tuple, Optional

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
from pymix.model.original_track_meta import OriginalTracks

router = APIRouter()

logger = logging.getLogger(__name__)

class ClientTracks(BaseModel):
    tracks: list[Dict[str, str]]

class Track(BaseModel):
    title: str
    artist: str
    album: Optional[str] = None

class MatchedTrack(BaseModel):
    title: str
    artist: str
    matched: bool

class Tracks(BaseModel):
    tracks: List[Track]

class MatchedTracksResponse(BaseModel):
    success: bool
    reason: str
    tracks: List[MatchedTrack]



@router.post("/sync/map_meta", tags=["sync"])
@inject
async def map_meta(
        tracks: OriginalTracks,
        session_id: str | None = Cookie(None),
        username: str | None = None,
        db_controller: DbController = Depends(Provide[Container.db_controller]),
        fb_file_handler: FileBrowserFileHandler = Depends(Provide[Container.file_browser_file_handler])
) -> dict:

    success = False
    reason = ""
    user = None
    if not username and not session_id:
        reason = "must have a session id to identify user"
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
    if user:
        await fb_file_handler.tag_staging_with_subbox_id(user['username'], tracks)
        untagged_tracks = list(filter(lambda t: t.subbox_id is None, tracks.tracks))
        assert len(untagged_tracks) == 0, f"untagged tracks for {untagged_tracks}"
        db_controller.save_original_track_meta(user['username'], tracks)


    return {
        'success': success
    }
@router.post("/sync/match_tracks", tags=["sync"])
@inject
async def match_tracks(
        tracks: Tracks,
        session_id: str | None = Cookie(None),
        username: str | None = None,
        db_controller: DbController = Depends(Provide[Container.db_controller]),
        subsonic_client: SubsonicClient = Depends(Provide[Container.subsonic_client])
) -> MatchedTracksResponse:

    success = False
    reason = ""
    matched_tracks = []
    user = None
    if not username and not session_id:
        reason = "must have a session id to identify user"
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
    if user:
        for track in tracks.tracks:
            match = await subsonic_client.get_track_match(user, track.title, track.artist, track.album)

            if match:
                match = match[0]
                logger.info(f'matched track {track} with {match}')
                matched_tracks.append(MatchedTrack(
                    title=match.name,
                    artist=match.artist,
                    matched=True
                ))
            else:
                matched_tracks.append(MatchedTrack(
                    title=track.title,
                    artist=track.artist,
                    matched=False
                ))
        success = True
    return MatchedTracksResponse(
        success=success,
        reason=reason,
        tracks=matched_tracks
    )

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
        client_sub_tracks = {}
        match_similarities = {}
        for client_track in client_tracks.tracks:
            sub_track = await subsonic_client.get_track_match(
                user=user,
                title=client_track['title'],
                artist=client_track['artist'],
                album=client_track.get('album'),
            )
            # if have an exact match then can exclude it as the client already having it.
            if sub_track:
                sub_track, similarity = sub_track
                assert sub_track.sub_track_id is not None
                if sub_track.sub_track_id in client_sub_tracks:
                    if similarity > match_similarities.get(sub_track.sub_track_id, 0):
                        client_sub_tracks[sub_track.sub_track_id] = sub_track
                        match_similarities[sub_track.sub_track_id] = similarity
                else:
                    client_sub_tracks[sub_track.sub_track_id] = sub_track
                    match_similarities[sub_track.sub_track_id] = similarity


        async for server_tracks in subsonic_client.get_all_tracks(user, 200):
            for server_track in server_tracks:
                server_tracks_dict[server_track.sub_track_id] = server_track

        logger.info(f'got {len(client_tracks.tracks)} raw client tracks')
        logger.info(f'got {len(server_tracks_dict)} from the server')
        logger.info(f'got {len(client_sub_tracks)} client matches')


        for client_track in client_sub_tracks.values():
            if client_track.sub_track_id not in server_tracks_dict:
                logger.debug(f'client match {client_track} not present in server tracks')
                client_tracks_to_remove.append(client_track)
            else:
                logger.debug(f'track {client_track} is present on both server and client')
                server_tracks_dict.pop(client_track.sub_track_id)

        logger.info(f'missing server tracks {list(server_tracks_dict.values())}')
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

