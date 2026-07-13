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
    # SUBBOX_ID read off the local file's tags, when the client has it. When present,
    # sync_plan matches on it directly (O(1) exact match) instead of fuzzy title/artist
    # matching. Absent for local tracks never tagged (e.g. imported from elsewhere and
    # not yet synced through subbox) — those still fall back to fuzzy matching.
    subboxId: Optional[str] = None

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

    # Resolved once for the whole request: each local track is matched against every
    # selected playlist, so re-deriving (title, artist) from the same Track for every
    # playlist was pure waste. Skipped entirely for tracks with a subboxId — they're
    # matched by id below and never touch the fuzzy (title, artist) path.
    resolved_locals = [
        (local, None, None)
        if local.subboxId
        else (local, *_resolve_local_track_for_matching(local))
        for local in request.localTracks
    ]
    n_subbox_id_locals = sum(1 for local, *_ in resolved_locals if local.subboxId)
    if n_subbox_id_locals:
        logger.info(
            "sync_plan: %s/%s local tracks carry a subboxId and will be matched exactly",
            n_subbox_id_locals,
            len(resolved_locals),
        )

    # subbox_ids of server tracks actually in the requested playlist(s) that carry a tag —
    # informational only (logged at INFO below), scoped to what was requested rather than
    # the user's whole local library (which is almost always much bigger than the
    # playlist(s) being synced).
    server_subbox_ids_seen: set[str] = set()

    # The precise divergence signal: a server track that (a) carries a subbox_id and
    # (b) still ends up classified "missing" after both the subbox_id fast path and the
    # fuzzy fallback have had a chance to match it (see the "missing" branch in
    # _process_server_tracks below). That combination is the only case that actually
    # causes a re-download of a track the user already has — unlike simply "this
    # server track's subbox_id wasn't found on any local file," which is equally true
    # for a server track any untagged local file will still fuzzy-match just fine.
    subbox_id_tagged_missing: set[str] = set()

    async def _process_server_tracks(server_tracks: List, context_label: str):
        matched_server_track_ids: set[int] = set()
        # A playlist may legitimately list the same song more than once (e.g. a duplicate
        # entry in Rekordbox). Each occurrence becomes its own server_tracks entry sharing
        # one subbox_id, but only one local file can ever back that id — track matched ids
        # by value too, so every duplicate entry is satisfied by a single local copy
        # instead of the object-identity check above leaving all-but-one falsely "missing".
        matched_subbox_ids: set[str] = set()
        summary["tracksRequested"] += len(server_tracks)

        # Local tracks tagged with a subboxId are matched directly against the server
        # tracks' own (already-parsed) subbox_id — an O(1) dict lookup per local track,
        # no fuzzy comparison needed. Only local tracks with no subboxId (never tagged)
        # fall back to fuzzy title/artist matching below.
        server_tracks_by_subbox_id = {
            track.subbox_id: track for track in server_tracks if track.subbox_id
        }
        server_subbox_ids_seen.update(server_tracks_by_subbox_id.keys())

        # Cleaned (title, artist, album, bracket-stripped title) depends only on the
        # server track, not on which local track it's being compared to — precompute it
        # once per server track instead of once per (local track, server track) pair.
        # Only needed for the fuzzy fallback, so skip it if every local track has an id.
        server_track_cleans = (
            None
            if n_subbox_id_locals == len(resolved_locals)
            else [SubsonicClient._clean_track_for_match(track) for track in server_tracks]
        )

        for local, local_title_for_match, local_artist_for_match in resolved_locals:
            matched_server_track = None
            similarity = None
            matched_via = "fuzzy"

            if local.subboxId:
                matched_via = "subbox_id"
                matched_server_track = server_tracks_by_subbox_id.get(local.subboxId)
                if matched_server_track:
                    similarity = 1.0
            else:
                match = await subsonic_client._get_best_track_match(
                    local_title_for_match,
                    local_artist_for_match,
                    local.album,
                    server_tracks,
                    similarity_threshold=0.6,
                    track_cleans=server_track_cleans,
                )
                if match:
                    matched_server_track, similarity = match

            if not matched_server_track:
                logger.info(
                    "sync_plan local_track_unmatched: user=%s context=%s via=%s local_raw=(%r,%r,%r,fromTag=%s) local_for_match=(%r,%r,%r)",
                    username,
                    context_label,
                    matched_via,
                    local.title,
                    local.artist,
                    local.album,
                    local.fromTag,
                    local_title_for_match,
                    local_artist_for_match,
                    local.album,
                )
                continue

            matched_server_track_ids.add(id(matched_server_track))
            if matched_via == "subbox_id":
                matched_subbox_ids.add(matched_server_track.subbox_id)
            logger.info(
                "sync_plan local_track_matched: user=%s context=%s via=%s local_raw=(%r,%r,%r,fromTag=%s) local_for_match=(%r,%r,%r) server=(%r,%r,%r) similarity=%.3f",
                username,
                context_label,
                matched_via,
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
            if id(track) in matched_server_track_ids or (
                track.subbox_id and track.subbox_id in matched_subbox_ids
            ):
                tracks["existing"].append({
                    "title": track.name,
                    "artist": track.artist,
                    "album": track.album,
                    "status": "match"
                })
                summary["tracksAlreadyPresent"] += 1
                continue

            if track.subbox_id:
                # This exact track is already tagged (pymix has seen it before), yet
                # neither the subbox_id fast path nor the fuzzy fallback found it among
                # the local tracks we were sent — the one combination that actually
                # causes a re-download of a track the user already has.
                subbox_id_tagged_missing.add(track.subbox_id)

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

    if server_subbox_ids_seen:
        # Informational visibility only, not an error signal — how many requested
        # server tracks are tagged. See subbox_id_divergence below for the actual
        # error-worthy signal.
        logger.info(
            "sync_plan subbox_id_summary: user=%s server_tracks_tagged=%s",
            username,
            len(server_subbox_ids_seen),
        )

    if subbox_id_tagged_missing:
        # The precise divergence signal: these specific tracks are tagged (pymix has
        # seen them before) but ended up "missing" anyway — after both the subbox_id
        # fast path *and* the fuzzy fallback had a chance to find them. This is the
        # combination that actually causes a re-download of a track the user already
        # has, unlike a bare "subbox_id not found locally" (which is equally true, and
        # harmless, for any tagged server track whose local copy just isn't tagged).
        logger.error(
            "sync_plan subbox_id_divergence: user=%s count=%s tagged tracks ended up "
            "missing despite fuzzy fallback — likely a stale/duplicate local SUBBOX_ID "
            "tag, will be re-downloaded",
            username,
            len(subbox_id_tagged_missing),
        )

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


@router.post("/sync/tracks", tags=["sync"])
@inject
async def sync_tracks(
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
        "sync_tracks start: user=%s tracks_to_download=%s",
        username,
        len(request.tracksToDownload),
    )

    for requested_track in request.tracksToDownload:
        resolved_title, resolved_artist = _resolve_local_track_for_matching(requested_track)
        candidate_tracks = await subsonic_client.query_tracks_by(user, resolved_title, resolved_artist)
        match = await subsonic_client._get_best_track_match(
            resolved_title,
            resolved_artist,
            requested_track.album,
            candidate_tracks,
            similarity_threshold=0.6,
        )
        if not match:
            candidate_tracks = await subsonic_client.query_track_by_name(user, resolved_title)
            match = await subsonic_client._get_best_track_match(
                resolved_title,
                resolved_artist,
                requested_track.album,
                candidate_tracks,
                similarity_threshold=0.5,
            )
        if match:
            matched_server_track, similarity = match
            logger.info(
                "sync_tracks track_found: user=%s requested=(%r,%r,%r) server=(%r,%r,%r) similarity=%.3f",
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
                "sync_tracks track_not_found: user=%s requested=(%r,%r,%r) — not found on server",
                username,
                requested_track.title,
                requested_track.artist,
                requested_track.album,
            )

    n_tracks_zipped, zip_path = fb_file_handler.sync(
        username=username,
        tracks_to_zip=all_tracks_to_zip,
    )
    logger.info(
        "sync_tracks complete: user=%s n_tracks_exported=%s zip_path=%s",
        username,
        len(all_tracks_to_zip),
        zip_path,
    )

    return {
        "success": True,
        "nTracksExported": len(all_tracks_to_zip),
        "zipPath": zip_path,
        "reason": "",
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
        # Resolved once for the whole request, same as sync_plan: skipped entirely for
        # tracks with a subboxId, since those are matched by id below and never touch
        # the fuzzy (title, artist) path.
        resolved_locals = [
            (local_track, None, None)
            if local_track.subboxId
            else (local_track, *_resolve_local_track_for_matching(local_track))
            for local_track in request.localTracks
        ]
        n_subbox_id_locals = sum(1 for local_track, *_ in resolved_locals if local_track.subboxId)

        # See sync_plan for the reasoning: server_subbox_ids_seen is informational only
        # (scoped to what was requested); subbox_id_tagged_missing is the precise
        # divergence signal (a tagged track that's still ending up re-exported despite
        # both the subbox_id fast path and the fuzzy fallback having a chance at it).
        server_subbox_ids_seen: set[str] = set()
        subbox_id_tagged_missing: set[str] = set()

        for playlist in request.playlists:
            playlist_tracks = await subsonic_client.get_playlist_tracks(user, playlist["id"])
            matched_server_track_ids: set[int] = set()

            # Local tracks tagged with a subboxId are matched directly against the
            # server tracks' own (already-parsed) subbox_id — an O(1) dict lookup, no
            # fuzzy comparison needed. Only untagged local tracks fall back to fuzzy
            # title/artist matching below.
            playlist_tracks_by_subbox_id = {
                track.subbox_id: track for track in playlist_tracks if track.subbox_id
            }
            server_subbox_ids_seen.update(playlist_tracks_by_subbox_id.keys())
            playlist_track_cleans = (
                None
                if n_subbox_id_locals == len(resolved_locals)
                else [SubsonicClient._clean_track_for_match(track) for track in playlist_tracks]
            )

            for local_track, local_title_for_match, local_artist_for_match in resolved_locals:
                matched_server_track = None
                similarity = None
                matched_via = "fuzzy"

                if local_track.subboxId:
                    matched_via = "subbox_id"
                    matched_server_track = playlist_tracks_by_subbox_id.get(local_track.subboxId)
                    if matched_server_track:
                        similarity = 1.0
                else:
                    match = await subsonic_client._get_best_track_match(
                        local_title_for_match,
                        local_artist_for_match,
                        local_track.album,
                        playlist_tracks,
                        similarity_threshold=0.6,
                        track_cleans=playlist_track_cleans,
                    )
                    if match:
                        matched_server_track, similarity = match

                if matched_server_track:
                    matched_server_track_ids.add(id(matched_server_track))
                    logger.info(
                        "sync_playlists local_track_matched: user=%s playlist_id=%s via=%s local_raw=(%r,%r,%r,fromTag=%s) local_for_match=(%r,%r,%r) server=(%r,%r,%r) similarity=%.3f",
                        username,
                        playlist.get("id"),
                        matched_via,
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
                        "sync_playlists local_track_unmatched: user=%s playlist_id=%s via=%s local_raw=(%r,%r,%r,fromTag=%s) local_for_match=(%r,%r,%r)",
                        username,
                        playlist.get("id"),
                        matched_via,
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
            for track in filtered_tracks:
                if track.subbox_id:
                    # Tagged (pymix has seen it before) but still being re-exported —
                    # the one combination that actually causes a duplicate export.
                    subbox_id_tagged_missing.add(track.subbox_id)
            all_tracks.extend(filtered_tracks)

        if server_subbox_ids_seen:
            # Informational visibility only, not an error signal — see sync_plan.
            logger.info(
                "sync_playlists subbox_id_summary: user=%s server_tracks_tagged=%s",
                username,
                len(server_subbox_ids_seen),
            )

        if subbox_id_tagged_missing:
            logger.error(
                "sync_playlists subbox_id_divergence: user=%s count=%s tagged tracks "
                "ended up re-exported despite fuzzy fallback — likely a stale/duplicate "
                "local SUBBOX_ID tag",
                username,
                len(subbox_id_tagged_missing),
            )

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
