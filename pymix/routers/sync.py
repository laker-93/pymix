import logging
import os
from typing import Dict, Annotated, List, Tuple, Optional

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, Query, Cookie, HTTPException
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
    fromTag: bool = True
    fileExtension: Optional[str] = None
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

class SyncPlanResponse(BaseModel):
    summary: Dict[str, int]
    tracks: Dict[str, List[Dict[str, str]]]
    metadata: Dict[str, List[Dict[str, str]]]
    download: Dict[str, str]

class SyncPlanRequest(BaseModel):
    direction: str
    playlists: Optional[List[Dict[str, str]]] = None
    localTracks: List[Track]
    options: Optional[Dict[str, bool]] = None


class SyncRequest(BaseModel):
    tracksToDownload: List[Track]


def _normalize_sync_match_value(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def _extract_artist_title_from_path_title(raw_title: str) -> tuple[Optional[str], Optional[str]]:
    parts = raw_title.split(" - ", 2)
    if len(parts) == 3:
        _, artist, title = parts
        return artist.strip(), title.strip()
    return None, None


def _resolve_local_track_for_matching(local_track: Track) -> tuple[str, str]:
    if not local_track.fromTag:
        parsed_artist, parsed_title = _extract_artist_title_from_path_title(local_track.title)
        if parsed_artist and parsed_title:
            return parsed_title, parsed_artist
    return local_track.title, local_track.artist

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
        tag_report = fb_file_handler.tag_staging_with_subbox_id(user['username'], tracks)
        untagged_tracks = list(filter(lambda t: t.subbox_id is None, tracks.tracks))
        if untagged_tracks:
            logger.error(
                'map_meta failed: %s tracks untagged for user %s. report=%s',
                len(untagged_tracks),
                user['username'],
                tag_report,
            )
            raise HTTPException(
                status_code=400,
                detail={
                    'message': 'failed to tag all staging tracks with SUBBOX_ID',
                    'untagged_count': len(untagged_tracks),
                    'untagged_tracks': [
                        {
                            'stagingLocation': t.stagingLocation,
                            'originalName': t.originalName,
                            'originalArtist': t.originalArtist,
                        }
                        for t in untagged_tracks
                    ],
                    'tag_report': tag_report,
                },
            )
        db_controller.save_original_track_meta(user['username'], tracks)
        success = True

    return {
        'success': success,
        'reason': reason
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

@router.post("/sync/plan", tags=["sync"])
@inject
async def sync_plan(
        request: SyncPlanRequest,
        session_id: str | None = Cookie(None),
        username: str | None = None,
        db_controller: DbController = Depends(Provide[Container.db_controller]),
        subsonic_client: SubsonicClient = Depends(Provide[Container.subsonic_client])
) -> SyncPlanResponse:
    if not username and not session_id:
        raise HTTPException(status_code=400, detail="Must have a username or session ID to identify user")

    user = None
    if username:
        user = db_controller.get_user(username)
    elif session_id:
        user = db_controller.get_user_by_session_id(session_id)
        username = user['username']

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    summary = {
        "playlists": len(request.playlists) if request.playlists else 0,
        "tracksRequested": 0,
        "tracksAlreadyPresent": 0,
        "tracksMissing": 0,
        "metadataUpdates": 0,
        "downloadSizeBytes": 0
    }
    tracks = {"missing": [], "existing": [], "conflicts": []}
    metadata = {"updates": []}
    download = {"strategy": "zip"}

    logger.info(
        "sync_plan start: user=%s direction=%s playlists=%s local_tracks=%s include_metadata=%s",
        username,
        request.direction,
        len(request.playlists) if request.playlists else "all",
        len(request.localTracks),
        bool(request.options and request.options.get("includeMetadata")),
    )

    async def _process_server_tracks(server_tracks: List, context_label: str):
        matched_server_track_ids: set[int] = set()
        summary["tracksRequested"] += len(server_tracks)
        for local in request.localTracks:
            local_title_for_match, local_artist_for_match = _resolve_local_track_for_matching(local)
            match = await subsonic_client._get_best_track_match(
                local_title_for_match,
                local_artist_for_match,
                local.album,
                server_tracks,
                similarity_threshold=0.6,
            )
            if not match:
                logger.info(
                    "sync_plan local_track_unmatched: user=%s context=%s local_raw=(%r,%r,%r,fromTag=%s) local_for_match=(%r,%r,%r)",
                    username,
                    context_label,
                    local.title,
                    local.artist,
                    local.album,
                    local.fromTag,
                    local_title_for_match,
                    local_artist_for_match,
                    local.album,
                )
                continue

            matched_server_track, similarity = match
            matched_server_track_ids.add(id(matched_server_track))
            logger.info(
                "sync_plan local_track_matched: user=%s context=%s local_raw=(%r,%r,%r,fromTag=%s) local_for_match=(%r,%r,%r) server=(%r,%r,%r) similarity=%.3f",
                username,
                context_label,
                local.title,
                local.artist,
                local.album,
                local.fromTag,
                local_title_for_match,
                local_artist_for_match,
                local.album,
                matched_server_track.name,
                matched_server_track.artist,
                matched_server_track.album,
                similarity,
            )

        for track in server_tracks:
            if id(track) in matched_server_track_ids:
                tracks["existing"].append({
                    "title": track.name,
                    "artist": track.artist,
                    "album": track.album,
                    "status": "match"
                })
                summary["tracksAlreadyPresent"] += 1
                continue

            file_size = 0
            if track.pymix_path and os.path.isfile(track.pymix_path):
                file_size = os.path.getsize(track.pymix_path)
            logger.info(
                "sync_plan missing: user=%s context=%s server=(%r,%r,%r) file_size=%s",
                username,
                context_label,
                track.name,
                track.artist,
                track.album,
                file_size,
            )
            tracks["missing"].append({
                "title": track.name,
                "artist": track.artist,
                "album": track.album,
            })
            summary["tracksMissing"] += 1
            summary["downloadSizeBytes"] += file_size

    if request.playlists:
        for playlist in request.playlists:
            playlist_tracks = await subsonic_client.get_playlist_tracks(user, playlist["id"])
            logger.info(
                "sync_plan playlist: user=%s playlist_id=%s playlist_name=%s server_tracks=%s",
                username,
                playlist.get("id"),
                playlist.get("name"),
                len(playlist_tracks),
            )
            await _process_server_tracks(playlist_tracks, playlist.get("id", "unknown"))
    else:
        logger.info("sync_plan: no playlists specified, iterating all server tracks")
        async for batch in subsonic_client.get_all_tracks(user, batch_size=500):
            await _process_server_tracks(batch, "all_tracks")

    if request.options and request.options.get("includeMetadata"):
        for track in tracks["missing"]:
            metadata["updates"].append({
                "title": track["title"],
                "artist": track["artist"],
            })
            summary["metadataUpdates"] += 1

    logger.info(
        "sync_plan complete: user=%s playlists=%s requested=%s existing=%s missing=%s metadata_updates=%s download_size_bytes=%s",
        username,
        summary["playlists"],
        summary["tracksRequested"],
        summary["tracksAlreadyPresent"],
        summary["tracksMissing"],
        summary["metadataUpdates"],
        summary["downloadSizeBytes"],
    )

    return SyncPlanResponse(
        summary=summary,
        tracks=tracks,
        metadata=metadata,
        download=download
    )

class SyncPlaylistArgs(BaseModel):
    ids: list[str]
    tracks: list[Dict[str, str]] = None


@router.post("/sync", tags=["sync"])
@inject
async def sync(
        request: SyncRequest,
        session_id: str | None = Cookie(None),
        username: str | None = None,
        db_controller: DbController = Depends(Provide[Container.db_controller]),
        fb_file_handler: FileBrowserFileHandler = Depends(Provide[Container.file_browser_file_handler]),
        subsonic_client: SubsonicClient = Depends(Provide[Container.subsonic_client])
) -> dict:
    if not username and not session_id:
        return {"success": False, "reason": "Must have a username or session ID to identify user"}

    user = None
    if username:
        try:
            user = db_controller.get_user(username)
        except Exception as ex:
            logger.error(f"Error occurred getting user for {username}", exc_info=True)
            return {"success": False, "reason": repr(ex)}
    elif session_id:
        try:
            user = db_controller.get_user_by_session_id(session_id)
            username = user["username"]
        except Exception as ex:
            logger.error(f"Error occurred getting user for session ID {session_id}", exc_info=True)
            return {"success": False, "reason": repr(ex)}

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    all_tracks_to_zip = []

    logger.info(
        "sync start: user=%s tracks_to_download=%s",
        username,
        len(request.tracksToDownload),
    )

    for requested_track in request.tracksToDownload:
        match = await subsonic_client.get_track_match(
            user,
            requested_track.title,
            requested_track.artist,
            requested_track.album,
        )
        if match:
            matched_server_track, similarity = match
            logger.info(
                "sync track_found: user=%s requested=(%r,%r,%r) server=(%r,%r,%r) similarity=%.3f",
                username,
                requested_track.title,
                requested_track.artist,
                requested_track.album,
                matched_server_track.name,
                matched_server_track.artist,
                matched_server_track.album,
                similarity,
            )
            all_tracks_to_zip.append(matched_server_track)
        else:
            logger.warning(
                "sync track_not_found: user=%s requested=(%r,%r,%r) — not found on server",
                username,
                requested_track.title,
                requested_track.artist,
                requested_track.album,
            )

    n_tracks_zipped, zip_path = fb_file_handler.sync(
        username=username,
        tracks_to_zip=all_tracks_to_zip
    )
    logger.info(
        "sync complete: user=%s n_tracks_exported=%s zip_path=%s",
        username,
        len(all_tracks_to_zip),
        zip_path,
    )

    return {
        "success": True,
        "nTracksExported": len(all_tracks_to_zip),
        "zipPath": zip_path,
        "reason": ""
    }


@router.post("/sync/playlists", tags=["sync"])
@inject
async def sync_playlists(
        request: SyncPlanRequest,
        session_id: str | None = Cookie(None),
        username: str | None = None,
        db_controller: DbController = Depends(Provide[Container.db_controller]),
        fb_file_handler: FileBrowserFileHandler = Depends(Provide[Container.file_browser_file_handler]),
        subsonic_client: SubsonicClient = Depends(Provide[Container.subsonic_client])
) -> dict:
    success = False
    reason = ""
    user = None
    zip_path = None
    all_tracks = []

    if not username and not session_id:
        return {"success": False, "reason": "Must have a username or session ID to identify user"}

    if username:
        try:
            user = db_controller.get_user(username)
        except Exception as ex:
            logger.error(f"Error occurred getting user for {username}", exc_info=True)
            return {"success": False, "reason": repr(ex)}
    elif session_id:
        try:
            user = db_controller.get_user_by_session_id(session_id)
            username = user["username"]
        except Exception as ex:
            logger.error(f"Error occurred getting user for session ID {session_id}", exc_info=True)
            return {"success": False, "reason": repr(ex)}

    if user:
        for playlist in request.playlists:
            playlist_tracks = await subsonic_client.get_playlist_tracks(user, playlist["id"])
            matched_server_track_ids: set[int] = set()
            for local_track in request.localTracks:
                local_title_for_match, local_artist_for_match = _resolve_local_track_for_matching(local_track)
                match = await subsonic_client._get_best_track_match(
                    local_title_for_match,
                    local_artist_for_match,
                    local_track.album,
                    playlist_tracks,
                    similarity_threshold=0.6,
                )
                if match:
                    matched_server_track, similarity = match
                    matched_server_track_ids.add(id(matched_server_track))
                    logger.info(
                        "sync_playlists local_track_matched: user=%s playlist_id=%s local_raw=(%r,%r,%r,fromTag=%s) local_for_match=(%r,%r,%r) server=(%r,%r,%r) similarity=%.3f",
                        username,
                        playlist.get("id"),
                        local_track.title,
                        local_track.artist,
                        local_track.album,
                        local_track.fromTag,
                        local_title_for_match,
                        local_artist_for_match,
                        local_track.album,
                        matched_server_track.name,
                        matched_server_track.artist,
                        matched_server_track.album,
                        similarity,
                    )
                else:
                    logger.info(
                        "sync_playlists local_track_unmatched: user=%s playlist_id=%s local_raw=(%r,%r,%r,fromTag=%s) local_for_match=(%r,%r,%r)",
                        username,
                        playlist.get("id"),
                        local_track.title,
                        local_track.artist,
                        local_track.album,
                        local_track.fromTag,
                        local_title_for_match,
                        local_artist_for_match,
                        local_track.album,
                    )

            filtered_tracks = [
                track for track in playlist_tracks
                if id(track) not in matched_server_track_ids
            ]
            all_tracks.extend(filtered_tracks)

        n_tracks_zipped, zip_path = fb_file_handler.sync(
            username=username,
            tracks_to_zip=all_tracks
        )
        success = True

    return {
        "success": success,
        "nTracksExported": len(all_tracks),
        "zipPath": zip_path,
        "reason": reason
    }
