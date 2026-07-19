from fastapi import APIRouter, Depends, Body, Path, Cookie, HTTPException
from jsonschema import validate, ValidationError
from dependency_injector.wiring import inject, Provide
from typing import Dict, Any, List
import logging

from pydantic import BaseModel

from pymix.containers import Container
from pymix.controllers.db_controller import DbController
from pymix.controllers.rekordbox_xml_controller import RekordboxXMLController
from pymix.routers.auth import require_username


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
        "key": {"type": "string"}
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
        username: str = Depends(require_username),
        db_controller: DbController = Depends(Provide[Container.db_controller]),
        rekordbox_xml_controller: RekordboxXMLController = Depends(Provide[Container.rekordbox_xml_controller]),
) -> Dict[str, Any]:
    # Batched, reorder-safe delete. The dangerous state (laker-93/pymix#30) was
    # committing the DB rows *before* the file/beets removal, so a failed beets
    # removal orphaned the track — file + Navidrome entry left behind with no pymix
    # mapping to reconcile from. Here we remove from beets first, verify the actual
    # end state, and only then delete the DB rows for ids that are confirmed gone.
    # Every beets step is a single OR-query, so the whole request is ~2-3 docker
    # execs regardless of how many ids were selected (was one `beet rm` per id).
    ids = list(dict.fromkeys(req.ids))  # de-dup, preserve order
    if not ids:
        return {"username": username, "success": True, "results": []}

    removal_reason = ""

    # 1️⃣ Which ids does beets actually have? Ids already absent are treated as
    #    "already in the desired end state" — an idempotent success, not a failure
    #    (this is the stale/desync case that used to false-report a failure).
    try:
        present = await rekordbox_xml_controller.get_present_subbox_ids(
            username=username, subbox_ids=ids, public=False
        )
    except Exception as ex:
        # Can't even determine beets state — refuse to touch the DB so nothing is
        # orphaned. Fail the whole batch; the client shows an error and can retry.
        reason = f"Error querying beets state for user {username}: {repr(ex)}"
        logger.error(reason, exc_info=True)
        return {
            "username": username,
            "success": False,
            "results": [{"subbox_id": i, "reason": reason, "success": False} for i in ids],
        }

    # 2️⃣ Remove the present ids from beets + disk in one batched command.
    if present:
        try:
            await rekordbox_xml_controller.remove_tracks(
                username=username, subbox_ids=sorted(present), public=False
            )
        except Exception as ex:
            # Don't trust the batch's success/failure — a batched rm can partially
            # succeed. Record the reason and fall through to verify the real state.
            removal_reason = f"Error removing tracks for user {username}: {repr(ex)}"
            logger.error(removal_reason, exc_info=True)

    # 3️⃣ Verify: re-query beets. Anything still present was NOT removed.
    try:
        still_present = await rekordbox_xml_controller.get_present_subbox_ids(
            username=username, subbox_ids=sorted(present), public=False
        )
    except Exception as ex:
        # Verification failed — be conservative and leave every present id's DB rows
        # intact (treat them all as not-removed) so nothing is orphaned.
        logger.error(
            f"Error verifying beets removal for user {username}: {repr(ex)}", exc_info=True
        )
        still_present = set(present)

    # 4️⃣ An id is "gone" if beets no longer has it (absent-at-start OR just removed).
    #    Delete DB rows only for those; leave rows for still-present ids so a retry
    #    can finish the job (no orphan).
    all_success = True
    results = []
    for subbox_id in ids:
        if subbox_id in still_present:
            all_success = False
            reason = removal_reason or (
                f"Track {subbox_id} still present in beets after removal for user {username}"
            )
            logger.warning(reason)
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
        "results": results
    }
