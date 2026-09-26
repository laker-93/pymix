from fastapi import APIRouter, Depends, Body, Path, Cookie, HTTPException
from jsonschema import validate, ValidationError
from dependency_injector.wiring import inject, Provide
from typing import Dict, Any, List
import logging

from pydantic import BaseModel

from pymix.containers import Container
from pymix.controllers.db_controller import DbController
from pymix.routers.auth import require_username, require_uploader
from pymix.services.trash import TrashService


class TrackPresenceRequest(BaseModel):
    subbox_ids: List[str]

    model_config = {"json_schema_extra": {"example": {"subbox_ids": ["uuid1", "uuid2"]}}}


_PRESENCE_MAX_IDS = 1000


class TrackPresenceResponse(BaseModel):
    presence: Dict[str, bool]

logger = logging.getLogger(__name__)
router = APIRouter()


cue_schema = {
    "type": "object",
    "properties": {
        "cues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},  # renamed from hotcue
                    "position": {"type": "number"},
                    "name": {"type": "string"},  # renamed from label
                    "color": {"type": "string"}
                },
                "required": ["index", "position"],
                "additionalProperties": False
            },
            "minItems": 0  # cues can be empty
        },
        "loops": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},  # added index
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "active": {"type": "boolean"}
                },
                "required": ["index", "start", "end"],
                "additionalProperties": False
            },
            "minItems": 0  # loops can be empty
        },
        "bpm": {"type": "number"},
        "key": {"type": "string"},
        # A grid is a list of anchors; see pymix/model/beatgrid.py for why the
        # last one is structurally different from the rest. `bpm` is optional
        # per-anchor because a Serato-sourced anchor carries a beat count
        # instead, and `beats_till_next` is absent on the anchor that carries a
        # tempo -- so neither can be required, and a marker needs only a
        # position to be worth storing.
        "beatgrid": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "position_ms": {"type": "number"},
                    "beats_till_next": {"type": ["integer", "null"]},
                    "bpm": {"type": ["number", "null"]},
                    "metro": {"type": "string"},
                    "battito": {"type": "integer"}
                },
                "required": ["position_ms"],
                "additionalProperties": False
            },
            "minItems": 0
        }
    },
    "additionalProperties": False
}

@router.post("/tracks/presence", tags=["tracks"], response_model=TrackPresenceResponse)
@inject
async def get_tracks_presence(
    body: TrackPresenceRequest,
    username: str = Depends(require_username),
    db_controller: DbController = Depends(Provide[Container.db_controller]),
) -> TrackPresenceResponse:
    """
    Given a list of subbox_ids, return which are already present in the
    user's library. Intended for the client to determine which files still
    need to be uploaded.
    """
    if len(body.subbox_ids) > _PRESENCE_MAX_IDS:
        raise HTTPException(
            status_code=400,
            detail=f"Too many subbox_ids in a single request (max {_PRESENCE_MAX_IDS}). Split into smaller batches.",
        )

    presence = db_controller.get_subbox_ids_presence(username, body.subbox_ids)
    return TrackPresenceResponse(presence=presence)


@router.post("/track/metadata/update", tags=["metadata"])
@inject
async def update_metadata(
    cuedata: Dict[str, Any] = Body(..., description="Cue and loop metadata in JSON form"),
    subbox_id: str | None = Cookie(None),
    source_app: str = Body(..., description="Source application (serato or rekordbox)"),
    change_type: str = Body(..., description="Type of change ('upload', 'edit', 'sync', 'merge')"),
    username: str = Depends(require_username),
    db_controller: DbController = Depends(Provide[Container.db_controller])
) -> dict:
    """
    Update metadata for a specific track in the user's library.
    The metadata update is versioned and stored in the DB.
    """

    success = True
    reason = ""

    try:
        validate(instance=cuedata, schema=cue_schema)
    except ValidationError as e:
        logger.error(f"Invalid cuedata for subbox_id={subbox_id}: {e.message}")
        return {
            "success": False,
            "reason": f"Invalid cuedata: {e.message}",
            "subbox_id": subbox_id
        }

    # --- 2️⃣ Perform metadata update ---
    try:
        logger.info(f"Updating metadata for user={username}, subbox_id={subbox_id}, source_app={source_app}")
        db_controller.update_metadata(
            username=username,
            subbox_id=subbox_id,
            cuedata=cuedata,
            source_app=source_app,
            change_type=change_type
        )
    except Exception as ex:
        success = False
        reason = f"Error updating metadata for subbox_id={subbox_id}: {repr(ex)}"
        logger.error(reason, exc_info=True)
    else:
        logger.info(f"Metadata update successful for {subbox_id} (user={username})")

    # --- 3️⃣ Return response ---
    return {
        "success": success,
        "reason": reason,
        "username": username,
        "subbox_id": subbox_id,
        "source_app": source_app,
    }

@router.get("/track/metadata/{track_id}", tags=["metadata"])
@inject
async def get_metadata(
    track_id: str = Path(..., description="Subbox track ID"),
    username: str = Depends(require_username),
    db_controller: DbController = Depends(Provide[Container.db_controller])
) -> Dict[str, Any]:
    """
    Retrieve metadata for a given track in the user's library.
    Resolves the user from the `session_id` cookie and returns cue/loop
    metadata as JSON.
    """

    success = True
    reason = ""
    cuedata = None

    # --- 1️⃣ Retrieve metadata ---
    try:
        logger.info(f"Fetching metadata for user={username}, track_id={track_id}")
        library_entry = db_controller.get_library_entry(username=username, subbox_id=track_id)

        if library_entry is None:
            success = False
            reason = f"No metadata found for track_id={track_id}"
            logger.warning(reason)
        else:
            cuedata = library_entry["cuedata"]
    except Exception as ex:
        success = False
        reason = f"Error retrieving metadata for {track_id}: {repr(ex)}"
        logger.error(reason, exc_info=True)

    # --- 3️⃣ Return response ---
    return {
        "success": success,
        "reason": reason,
        "username": username,
        "track_id": track_id,
        "metadata": cuedata if success else None
    }


class DeleteTrackRequest(BaseModel):
    ids: List[str]
@router.delete("/track", tags=["metadata"])
@inject
async def delete_track(
        req: DeleteTrackRequest = Body(...),
        user: dict = Depends(require_uploader),
        db_controller: DbController = Depends(Provide[Container.db_controller]),
        trash_service: TrashService = Depends(Provide[Container.trash_service]),
) -> Dict[str, Any]:
    # Deletion is a library write, not just an upload/import one, but it's gated by
    # the same require_uploader dependency: `demo` may browse demoadmin's shared
    # library (via require_reader) but must never be able to remove tracks from it.
    # Only demoadmin (or a real account deleting its own tracks) reaches this point.
    username = user["username"]
    # A delete moves the tracks into the user's trash (#200): the file is kept, at
    # /private-music/_trash/{user}/{batch}/, until the reaper purges it, so a
    # mistaken delete can be undone. The ordering that fixed laker-93/pymix#30 holds:
    # the pymix rows are deleted only for ids beets is verified to no longer have,
    # so a failed removal never orphans a track. Every beets step is a single
    # OR-query, so a request is a handful of docker execs however many ids it names.
    ids = list(dict.fromkeys(req.ids))  # de-dup, preserve order
    if not ids:
        return {"username": username, "success": True, "results": [], "trash_batch_id": None}

    try:
        outcome = await trash_service.trash_tracks(username, ids)
    except Exception as ex:
        # beets could not be read, or the snapshot could not be taken. Nothing has
        # been touched; fail the whole request and let the client retry.
        reason = f"Error deleting tracks for user {username}: {repr(ex)}"
        logger.error(reason, exc_info=True)
        return {
            "username": username,
            "success": False,
            "results": [{"subbox_id": i, "reason": reason, "success": False} for i in ids],
            "trash_batch_id": None,
        }

    # An id is "gone" if beets no longer has it: absent to begin with (an idempotent
    # success, not the stale-desync failure it used to report) or just trashed.
    # Delete DB rows only for those; a still-present id keeps its rows, so a retry
    # can finish the job.
    all_success = True
    results = []
    for subbox_id in ids:
        if subbox_id in outcome.not_removed:
            all_success = False
            reason = outcome.not_removed[subbox_id]
            logger.warning(f"{username}: {reason}")
            results.append({"subbox_id": subbox_id, "reason": reason, "success": False})
            continue
        try:
            db_controller.delete_track(username=username, subbox_id=subbox_id)
            results.append({"subbox_id": subbox_id, "reason": "", "success": True})
        except Exception as ex:
            all_success = False
            reason = f"Error deleting DB rows for {subbox_id} for user {username}: {repr(ex)}"
            logger.error(reason, exc_info=True)
            results.append({"subbox_id": subbox_id, "reason": reason, "success": False})

    return {
        "username": username,
        "success": all_success,
        "results": results,
        # Additive: what the batch is called in GET /trash. None when nothing moved.
        "trash_batch_id": outcome.batch_id,
    }
